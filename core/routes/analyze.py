"""分析相关路由（智能分析、报告导出、数据大屏状态）。"""
import io
import json
import os
import re
import threading
import zipfile
from datetime import datetime

from flask import flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required


def register_analyze_routes(
    bp,
    *,
    SessionLocal,
    Task,
    Submission,
    AIConfig,
    OrganizationMember,
    TaskShare,
    decrypt_ai_config_inplace,
    call_ai_model,
    get_chat_server_model_light,
    generate_analysis_prompt,
    read_file_content,
    perform_analysis_with_custom_prompt,
    save_analysis_report,
    generate_report_image,
    build_report_html,
    _to_user_friendly_ai_error,
    _public_site_base_url,
    _static_uploads_dir,
    progress_lock,
    analysis_progress,
    SUPPORTED_AI_MODELS,
    MODEL_LABELS,
    logger,
):
    @bp.route('/analyze/<int:task_id>/smart_analyze', methods=['GET', 'POST'])
    @login_required
    def smart_analyze(task_id):
        """智能分析"""
        db = SessionLocal()
        try:
            task = db.get(Task, task_id)
            if not task:
                flash('任务不存在', 'danger')
                return redirect(url_for('quickform.dashboard'))

            # 权限检查：管理员、任务所有者、被共享者、组织成员可以生成分析报告
            has_access = False
            if current_user.is_admin() or task.user_id == current_user.id:
                has_access = True
            elif db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first():
                has_access = True
            elif task.organization_id:
                is_org_member = (
                    db.query(OrganizationMember)
                    .filter_by(organization_id=task.organization_id, user_id=current_user.id)
                    .first()
                    is not None
                )
                if is_org_member:
                    has_access = True

            if not has_access:
                flash('无权访问此任务', 'danger')
                return redirect(url_for('quickform.dashboard'))

            ai_config = db.query(AIConfig).filter_by(user_id=current_user.id).first()
            decrypt_ai_config_inplace(ai_config)
            model_label = None
            ai_ready = False
            ai_ready_reason = ''
            if ai_config and ai_config.selected_model:
                model_label = MODEL_LABELS.get(ai_config.selected_model, ai_config.selected_model)
                if ai_config.selected_model not in SUPPORTED_AI_MODELS:
                    ai_ready_reason = f"{model_label} 暂未集成，敬请期待后续版本"
                elif ai_config.selected_model == 'deepseek' and not ai_config.deepseek_api_key:
                    ai_ready_reason = "请先配置DeepSeek API密钥"
                elif ai_config.selected_model == 'doubao' and not ai_config.doubao_api_key:
                    ai_ready_reason = "请先配置豆包API密钥"
                else:
                    ai_ready = True
            else:
                ai_ready_reason = "请先在个人设置中配置AI模型和API密钥"

            # 数据概览：供「数据分析向导」使用（尽量从数据库取样，避免 /all 压力）
            submission_q = db.query(Submission).filter_by(task_id=task_id)
            current_submission_count = submission_q.count()
            sample_n = 3
            try:
                sample_n_env = int((os.getenv('QF_DASHBOARD_SAMPLE_N') or '').strip() or '3')
                sample_n = max(1, min(8, sample_n_env))
            except Exception:
                sample_n = 3
            sample_rows = submission_q.order_by(Submission.id.asc()).limit(sample_n).all()
            samples = []
            parsed_samples = []
            for r in (sample_rows or []):
                s = ((getattr(r, 'data', '') or '')).strip()
                if not s:
                    continue
                samples.append(s)
                try:
                    parsed_samples.append(json.loads(s))
                except Exception:
                    pass
            if parsed_samples:
                try:
                    sample_payload = {
                        'submissions': parsed_samples,
                        'total_submissions': int(current_submission_count or len(parsed_samples) or 0),
                    }
                    sample_data_raw = json.dumps(sample_payload, ensure_ascii=True, indent=2)
                except Exception:
                    sample_data_raw = "[\n" + ",\n".join(samples) + "\n]" if samples else ''
            elif samples:
                sample_data_raw = "[\n" + ",\n".join(samples) + "\n]"
            else:
                sample_data_raw = ''
            if len(sample_data_raw) > 6000:
                sample_data_raw = sample_data_raw[:6000] + '...'
            api_base_url = _public_site_base_url()
            all_url = f"{api_base_url}/api/{task.task_id}/all"
            stable_dash_saved = f"dash_{task.task_id}.html"

            if request.method == 'POST':
                form_action = (request.form.get('action') or '').strip()

                if form_action in ('dashboard_generate', 'dashboard_revise'):
                    if current_submission_count <= 0:
                        flash('你至少需要回收一条数据。', 'warning')
                        return redirect(url_for('quickform.smart_analyze', task_id=task.id))
                    if not ai_ready:
                        flash(ai_ready_reason or '请先配置 AI 后再自动生成。', 'warning')
                        return redirect(url_for('quickform.smart_analyze', task_id=task.id))

                    remaining = getattr(task, 'dashboard_ai_edit_remaining', None)
                    if remaining is None:
                        remaining = 3
                    try:
                        remaining = int(remaining)
                    except Exception:
                        remaining = 0
                    if remaining <= 0:
                        flash('仅允许 3 次自动生成/修改，当前次数已用完。', 'warning')
                        return redirect(url_for('quickform.smart_analyze', task_id=task.id))

                    user_prompt = (request.form.get('dashboard_user_prompt') or '').strip()
                    revise_ins = (request.form.get('dashboard_revision') or '').strip()
                    base_html_upload = request.files.get('dashboard_base_html')
                    base_html_text = ''
                    if form_action == 'dashboard_generate':
                        if not base_html_upload or not getattr(base_html_upload, 'filename', ''):
                            flash('请先上传你的学生端 HTML 文件（必填）。', 'warning')
                            return redirect(url_for('quickform.smart_analyze', task_id=task.id))
                        try:
                            raw = base_html_upload.read() or b''
                            max_bytes = 24000
                            head_bytes = raw[:max_bytes]
                            tail_bytes = raw[-8000:] if len(raw) > (max_bytes + 8000) else b''
                            base_html_text = head_bytes.decode('utf-8', errors='ignore')
                            if tail_bytes:
                                base_html_text += "\n\n<!-- (中间内容省略) -->\n\n" + tail_bytes.decode('utf-8', errors='ignore')
                            base_html_text = (base_html_text or '').strip()
                        except Exception:
                            base_html_text = ''

                    base_prompt = (
                        "你是一名资深前端工程师。请生成一个单页 HTML（只输出完整 HTML）。\n"
                        "目标：制作一个可以实时分析统计的数据大屏（数据看板）。\n"
                        f"数据来自：{all_url}\n"
                        "要求：每隔 10 秒刷新一次（fetch 获取 JSON），并渲染关键指标与图表。\n"
                        "页面要求：手机端可用、布局清晰、默认浅色主题；不要依赖外部 CDN；所有逻辑写在同一个 HTML 文件里。\n"
                        "重要约束：不要使用 Chart.js / ECharts 等外部库名（例如 Chart、echarts），因为本页面不允许外部 CDN，且未内置这些库；\n"
                        "如需图表，请使用原生 Canvas（2D）自行绘制，或用纯 HTML/CSS 的条形/进度条等方式展示，避免出现“Chart is not defined”等运行错误。\n"
                        "数据格式如下（示例，字段可能更多，请自适应）：\n"
                        f"{sample_data_raw or '[暂无示例数据]'}\n"
                    )
                    if base_html_text:
                        base_prompt += (
                            "\n现有学生端页面（你需要以此为基础进行改造，尽量保留其样式与表单字段一致性；如下为节选）：\n"
                            + base_html_text
                            + "\n"
                        )
                    if user_prompt:
                        base_prompt += "\n用户需求补充：\n" + user_prompt + "\n"
                    if form_action == 'dashboard_revise' and revise_ins:
                        base_prompt += "\n在保留现有大屏功能的基础上，按以下说明修改：\n" + revise_ins + "\n"

                    task.dashboard_generation_status = 'pending'
                    task.dashboard_generation_error = None
                    task.dashboard_saved_name = task.dashboard_saved_name or stable_dash_saved
                    task.dashboard_ai_edit_remaining = max(0, remaining - 1)
                    db.commit()

                    def _dash_bg(task_pk: int, ai_cfg_id: int, prompt_text: str, saved_name: str):
                        _db = SessionLocal()
                        try:
                            tsk = _db.get(Task, task_pk)
                            cfg = _db.get(AIConfig, ai_cfg_id)
                            decrypt_ai_config_inplace(cfg)
                            html_text = call_ai_model(prompt_text, cfg, chat_server_model=get_chat_server_model_light())
                            html_text = (html_text or '').strip()
                            if html_text.startswith("```"):
                                html_text = html_text.strip('` \n')
                            static_uploads = _static_uploads_dir()
                            out_path = os.path.join(static_uploads, saved_name)
                            with open(out_path, 'w', encoding='utf-8') as f:
                                f.write(html_text)
                            tsk.dashboard_file_name = saved_name
                            tsk.dashboard_generated_at = datetime.now()
                            tsk.dashboard_generation_status = 'completed'
                            tsk.dashboard_generation_error = None
                            _db.commit()
                        except Exception as ex:
                            try:
                                tsk = _db.get(Task, task_pk)
                                tsk.dashboard_generation_status = 'failed'
                                friendly = _to_user_friendly_ai_error(str(ex))
                                tsk.dashboard_generation_error = (
                                    "自动生成失败：可能是提示词过长（包含学生端 HTML + 多条数据样例）超过模型上限。\n"
                                    "兜底方案：请改用「方式1：手动生成」，复制提示词模板到 AI 工具生成数据大屏。\n\n"
                                    + friendly
                                ) if '超过模型可处理上限' in friendly else friendly
                                if '超过模型可处理上限' in friendly:
                                    try:
                                        cur = getattr(tsk, 'dashboard_ai_edit_remaining', None)
                                        cur = int(cur) if cur is not None else 0
                                        tsk.dashboard_ai_edit_remaining = min(3, cur + 1)
                                    except Exception:
                                        pass
                                _db.commit()
                            except Exception:
                                _db.rollback()
                            logger.exception("数据大屏生成失败: %s", ex)
                        finally:
                            _db.close()

                    try:
                        t = threading.Thread(
                            target=_dash_bg,
                            args=(task.id, ai_config.id, base_prompt, task.dashboard_saved_name),
                            daemon=True,
                        )
                        t.start()
                        return redirect(url_for('quickform.smart_analyze', task_id=task.id, dash_running=1))
                    except Exception as e:
                        logger.exception("启动数据大屏生成线程失败: %s", e)
                        flash('无法启动数据大屏生成，请稍后重试。', 'danger')
                        return redirect(url_for('quickform.smart_analyze', task_id=task.id))

                # 检查是仅保存模板还是生成报告
                action = request.form.get('action', 'generate')
                report_context = (request.form.get('report_context', '') or '').strip()
                if not report_context:
                    legacy_interface_desc = (request.form.get('interface_desc', '') or '').strip()
                    legacy_focus = (request.form.get('user_prompt_template', '') or '').strip()
                    report_context = '\n'.join([x for x in [legacy_interface_desc, legacy_focus] if x]).strip()
                if report_context:
                    task.user_prompt_template = report_context
                    db.commit()
                if action == 'save_template':
                    flash('提示词模板已保存', 'success')
                    return redirect(url_for('quickform.smart_analyze', task_id=task.id))
                if not ai_ready:
                    flash(ai_ready_reason or '请先在个人设置中配置 AI 模型与 APIKEY。', 'warning')
                    return redirect(url_for('quickform.smart_analyze', task_id=task.id))

                submission_for_prompt = db.query(Submission).filter_by(task_id=task_id).all()
                data_range = request.form.get('data_range', 'all')
                if data_range == 'single_day':
                    single_date_str = request.form.get('single_date', '')
                    if single_date_str:
                        from datetime import datetime as dt

                        try:
                            target_date = dt.strptime(single_date_str, '%Y-%m-%d').date()
                            submission_for_prompt = [
                                s for s in submission_for_prompt if s.submitted_at and s.submitted_at.date() == target_date
                            ]
                        except (ValueError, TypeError):
                            pass
                elif data_range == 'date_range':
                    date_start_str = request.form.get('date_start', '')
                    date_end_str = request.form.get('date_end', '')
                    if date_start_str and date_end_str:
                        from datetime import datetime as dt

                        try:
                            start_d = dt.strptime(date_start_str, '%Y-%m-%d').date()
                            end_d = dt.strptime(date_end_str, '%Y-%m-%d').date()
                            submission_for_prompt = [
                                s
                                for s in submission_for_prompt
                                if s.submitted_at and start_d <= s.submitted_at.date() <= end_d
                            ]
                        except (ValueError, TypeError):
                            pass
                interface_desc = (task.description or '').strip()
                file_content_for_prompt = None
                if task.file_path and os.path.exists(task.file_path):
                    file_content_for_prompt = read_file_content(task.file_path)

                custom_prompt_from_form = request.form.get('custom_prompt', '').strip()
                if custom_prompt_from_form:
                    custom_prompt = custom_prompt_from_form
                else:
                    user_prompt_from_form = (request.form.get('report_context', '') or '').strip()
                    if not user_prompt_from_form:
                        user_prompt_from_form = (request.form.get('user_prompt_template', '') or '').strip()
                    user_template_val = user_prompt_from_form or (task.user_prompt_template if task.user_prompt_template else None)
                    custom_prompt = generate_analysis_prompt(
                        task,
                        submission_for_prompt,
                        file_content_for_prompt,
                        SessionLocal,
                        Submission,
                        user_template=user_template_val,
                        interface_desc=interface_desc or None,
                    )

                prompt_trimmed = False
                custom_prompt_to_save = custom_prompt or ''
                max_prompt_bytes = 60000
                prompt_bytes = custom_prompt_to_save.encode('utf-8', errors='ignore')
                if len(prompt_bytes) > max_prompt_bytes:
                    custom_prompt_to_save = prompt_bytes[:max_prompt_bytes].decode('utf-8', errors='ignore')
                    prompt_trimmed = True
                task.custom_prompt = custom_prompt_to_save
                try:
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logger.warning("保存 custom_prompt 失败，改为不落库继续生成: %s", e)
                    task.custom_prompt = None
                    db.commit()
                    prompt_trimmed = True
                if prompt_trimmed:
                    flash('数据量较大：提示词已自动裁剪后保存，不影响本次报告生成。', 'warning')

                try:
                    t = threading.Thread(
                        target=perform_analysis_with_custom_prompt,
                        args=(
                            task_id,
                            current_user.id,
                            ai_config.id,
                            custom_prompt,
                            SessionLocal,
                            Task,
                            Submission,
                            AIConfig,
                            read_file_content,
                            call_ai_model,
                            save_analysis_report,
                            None,
                            OrganizationMember,
                            TaskShare,
                        ),
                        daemon=True,
                    )
                    t.start()
                    return redirect(url_for('quickform.smart_analyze', task_id=task.id, running=1))
                except Exception as e:
                    logger.exception("启动报告生成线程失败: %s", e)
                    return render_template(
                        'smart_analyze.html',
                        task=task,
                        error='无法启动报告生成，请稍后重试。若持续失败请联系管理员。',
                        ai_config=ai_config,
                        now=datetime.now(),
                        model_label=model_label,
                    )

            db.refresh(task)
            submission = db.query(Submission).filter_by(task_id=task_id).all()
            current_submission_count = len(submission)
            file_content = None
            if task.file_path and os.path.exists(task.file_path):
                file_content = read_file_content(task.file_path)

            should_regenerate_prompt = False
            if task.custom_prompt and task.custom_prompt.strip():
                count_patterns = [
                    r'总提交数量[：:]\s*(\d+)\s*条',
                    r'共有\s*(\d+)\s*条提交记录',
                    r'总提交数量[：:]\s*(\d+)',
                ]
                saved_count = None
                for pattern in count_patterns:
                    match = re.search(pattern, task.custom_prompt)
                    if match:
                        saved_count = int(match.group(1))
                        break
                if saved_count is not None and saved_count != current_submission_count:
                    should_regenerate_prompt = True
                    logger.info("任务 %s 的数据条数已更新：%s -> %s，重新生成提示词", task_id, saved_count, current_submission_count)
            else:
                should_regenerate_prompt = True

            user_template = task.user_prompt_template if task.user_prompt_template else None
            interface_desc = (task.description or '').strip()
            if should_regenerate_prompt:
                preview_prompt = generate_analysis_prompt(
                    task, submission, file_content, SessionLocal, Submission, user_template=user_template, interface_desc=interface_desc
                )
            else:
                if user_template:
                    preview_prompt = generate_analysis_prompt(
                        task, submission, file_content, SessionLocal, Submission, user_template=user_template, interface_desc=interface_desc
                    )
                else:
                    preview_prompt = task.custom_prompt

            report = task.analysis_report if task and task.analysis_report else None
            user_prompt_template = task.user_prompt_template if task.user_prompt_template else ''

            running_flag = request.args.get('running') == '1'
            should_redirect = False
            if running_flag:
                with progress_lock:
                    prog = analysis_progress.get(task.id)
                if prog and prog.get('status') == 'completed':
                    should_redirect = True
            if should_redirect:
                return redirect(url_for('quickform.smart_analyze', task_id=task.id))

            return render_template(
                'smart_analyze.html',
                task=task,
                report=report,
                preview_prompt=preview_prompt,
                user_prompt_template=user_prompt_template,
                ai_config=ai_config,
                now=datetime.now(),
                model_label=model_label,
                submission_count=current_submission_count,
                is_large_dataset=current_submission_count > 200,
                ai_ready=ai_ready,
                ai_ready_reason=ai_ready_reason,
                all_url=all_url,
                sample_data_raw=sample_data_raw,
                dashboard_saved_name=getattr(task, 'dashboard_saved_name', None) or stable_dash_saved,
                dashboard_status=getattr(task, 'dashboard_generation_status', None),
                dashboard_error=getattr(task, 'dashboard_generation_error', None),
                dashboard_remaining=getattr(task, 'dashboard_ai_edit_remaining', None)
                if getattr(task, 'dashboard_ai_edit_remaining', None) is not None
                else 3,
                dash_running=(request.args.get('dash_running') == '1'),
            )
        finally:
            db.close()

    @bp.route('/download_report/<int:task_id>')
    @login_required
    def download_report(task_id):
        """下载报告 - 支持 PNG（长报告分多张）、HTML、PDF"""
        fmt = (request.args.get('format') or 'png').strip().lower()
        db = SessionLocal()
        try:
            task = db.get(Task, task_id)
            if not task:
                flash('任务不存在', 'danger')
                return redirect(url_for('quickform.dashboard'))

            has_access = False
            if current_user.is_admin() or task.user_id == current_user.id:
                has_access = True
            elif db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first():
                has_access = True
            elif task.organization_id:
                is_org_member = (
                    db.query(OrganizationMember)
                    .filter_by(organization_id=task.organization_id, user_id=current_user.id)
                    .first()
                    is not None
                )
                if is_org_member:
                    has_access = True

            if not has_access:
                flash('无权访问此任务', 'danger')
                return redirect(url_for('quickform.dashboard'))

            report_content = task.analysis_report or "暂无报告内容"
            safe_title = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', task.title)[:50]

            if fmt == 'html':
                html_str = build_report_html(task, report_content)
                response = make_response(html_str)
                response.headers['Content-Type'] = 'text/html; charset=utf-8'
                from urllib.parse import quote as _url_quote

                response.headers['Content-Disposition'] = (
                    "attachment; filename*=UTF-8''" + _url_quote((safe_title + '_report.html').encode('utf-8'))
                )
                return response

            if fmt == 'pdf':
                html_str = build_report_html(task, report_content, for_pdf=True)
                pdf_io = io.BytesIO()
                pdf_ok = False
                try:
                    from weasyprint import HTML as WeasyHTML

                    WeasyHTML(string=html_str).write_pdf(pdf_io)
                    pdf_ok = True
                except (ImportError, OSError, Exception) as e:
                    logger.warning("weasyprint 不可用（%s），尝试 xhtml2pdf", e)
                    try:
                        from xhtml2pdf import pisa

                        pdf_io = io.BytesIO()
                        pisa_status = pisa.CreatePDF(html_str, dest=pdf_io, encoding='utf-8')
                        if not pisa_status.err:
                            pdf_ok = True
                    except ImportError:
                        flash('PDF 导出需要安装 weasyprint 或 xhtml2pdf。Windows 推荐: pip install xhtml2pdf', 'warning')
                        return redirect(url_for('quickform.smart_analyze', task_id=task_id))
                    except Exception as e2:
                        logger.error("xhtml2pdf 生成 PDF 失败: %s", str(e2), exc_info=True)
                        flash(f'生成 PDF 时出错: {str(e2)}', 'danger')
                        return redirect(url_for('quickform.smart_analyze', task_id=task_id))
                if pdf_ok:
                    pdf_io.seek(0)
                    response = make_response(pdf_io.getvalue())
                    response.headers['Content-Type'] = 'application/pdf'
                    from urllib.parse import quote as _url_quote

                    response.headers['Content-Disposition'] = (
                        "attachment; filename*=UTF-8''" + _url_quote((safe_title + '_report.pdf').encode('utf-8'))
                    )
                    return response
                flash('PDF 生成失败', 'danger')
                return redirect(url_for('quickform.smart_analyze', task_id=task_id))

            buffers, filenames = generate_report_image(task, report_content)
            from urllib.parse import quote as _url_quote

            if len(buffers) == 1:
                response = make_response(buffers[0].getvalue())
                response.headers['Content-Type'] = 'image/png'
                response.headers['Content-Disposition'] = (
                    "attachment; filename*=UTF-8''" + _url_quote(filenames[0].encode('utf-8'))
                )
                return response
            zip_io = io.BytesIO()
            with zipfile.ZipFile(zip_io, 'w', zipfile.ZIP_DEFLATED) as zf:
                for buf, name in zip(buffers, filenames):
                    zf.writestr(name, buf.getvalue())
            zip_io.seek(0)
            response = make_response(zip_io.getvalue())
            response.headers['Content-Type'] = 'application/zip'
            response.headers['Content-Disposition'] = (
                "attachment; filename*=UTF-8''" + _url_quote((safe_title + '_report_图片.zip').encode('utf-8'))
            )
            return response
        except Exception as e:
            logger.exception("下载报告失败: %s", e)
            flash('下载报告失败，请稍后重试。', 'danger')
            return redirect(url_for('quickform.dashboard'))
        finally:
            db.close()

    @bp.route('/analyze/<int:task_id>/dashboard_status', methods=['GET'])
    @login_required
    def dashboard_status(task_id):
        """数据大屏生成状态（smart_analyze 页面轮询使用）"""
        db = SessionLocal()
        try:
            task = db.get(Task, task_id)
            if not task:
                return jsonify({'success': False, 'message': '任务不存在'}), 404
            has_access = False
            if current_user.is_admin() or task.user_id == current_user.id:
                has_access = True
            elif db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first():
                has_access = True
            elif task.organization_id:
                is_org_member = (
                    db.query(OrganizationMember)
                    .filter_by(organization_id=task.organization_id, user_id=current_user.id)
                    .first()
                    is not None
                )
                if is_org_member:
                    has_access = True
            if not has_access:
                return jsonify({'success': False, 'message': '无权访问此任务'}), 403

            api_base_url = _public_site_base_url()
            saved_name = getattr(task, 'dashboard_saved_name', None)
            dash_url = f"{api_base_url}/static/uploads/{saved_name}" if saved_name else None
            return jsonify(
                {
                    'success': True,
                    'status': getattr(task, 'dashboard_generation_status', None),
                    'error': getattr(task, 'dashboard_generation_error', None),
                    'remaining': getattr(task, 'dashboard_ai_edit_remaining', None),
                    'dash_url': dash_url,
                    'generated_at': task.dashboard_generated_at.strftime('%Y-%m-%d %H:%M:%S')
                    if getattr(task, 'dashboard_generated_at', None)
                    else None,
                }
            )
        finally:
            db.close()
