/**
 * QuickForm 表单增强功能
 * 1. 选项限制悬浮显示
 * 2. 表格类型界面缩放功能
 */

(function() {
    'use strict';

    // ==================== 选项限制悬浮显示 ====================
    class OptionLimitTracker {
        constructor() {
            this.limits = {};
            this.counts = {};
            this.tooltip = null;
            this.init();
        }

        init() {
            // 从data属性或全局变量获取选项限制
            this.loadLimits();
            
            // 监听所有选项的变化
            this.attachListeners();
            
            // 创建悬浮提示框
            this.createTooltip();
            
            // 初始更新
            this.updateCounts();
        }

        loadLimits() {
            // 方法1: 从全局变量获取
            if (typeof window.optionLimits !== 'undefined') {
                this.limits = window.optionLimits;
            }
            
            // 方法2: 从data属性获取
            const limitData = document.querySelector('[data-option-limits]');
            if (limitData) {
                try {
                    this.limits = JSON.parse(limitData.getAttribute('data-option-limits'));
                } catch (e) {
                    console.warn('无法解析选项限制数据', e);
                }
            }
            
            // 方法3: 从meta标签获取
            const metaLimit = document.querySelector('meta[name="option-limits"]');
            if (metaLimit) {
                try {
                    this.limits = JSON.parse(metaLimit.getAttribute('content'));
                } catch (e) {
                    console.warn('无法从meta标签解析选项限制', e);
                }
            }

            // 初始化计数
            Object.keys(this.limits).forEach(option => {
                this.counts[option] = 0;
            });
        }

        attachListeners() {
            // 监听所有单选按钮和复选框
            document.addEventListener('change', (e) => {
                const target = e.target;
                if (target.type === 'radio' || target.type === 'checkbox') {
                    this.updateCounts();
                }
            });

            // 监听表单重置
            document.addEventListener('reset', () => {
                setTimeout(() => this.updateCounts(), 100);
            });
        }

        updateCounts() {
            // 重置计数
            Object.keys(this.limits).forEach(option => {
                this.counts[option] = 0;
            });

            // 统计每个选项的选择次数
            document.querySelectorAll('input[type="radio"]:checked, input[type="checkbox"]:checked').forEach(input => {
                const value = input.value.toUpperCase();
                if (this.limits.hasOwnProperty(value)) {
                    this.counts[value] = (this.counts[value] || 0) + 1;
                }
            });

            // 更新悬浮提示
            this.updateTooltip();
        }

        createTooltip() {
            this.tooltip = document.createElement('div');
            this.tooltip.id = 'option-limit-tooltip';
            this.tooltip.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                background: rgba(0, 0, 0, 0.85);
                color: white;
                padding: 12px 16px;
                border-radius: 8px;
                font-size: 14px;
                z-index: 10000;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
                min-width: 200px;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                display: none;
                transition: opacity 0.3s ease;
            `;
            document.body.appendChild(this.tooltip);
        }

        updateTooltip() {
            if (!this.tooltip || Object.keys(this.limits).length === 0) {
                return;
            }

            let html = '<div style="font-weight: 600; margin-bottom: 8px; font-size: 15px;">选项限制状态</div>';
            let hasActiveLimits = false;

            Object.keys(this.limits).sort().forEach(option => {
                const limit = this.limits[option];
                const count = this.counts[option] || 0;
                
                if (limit && limit > 0) {
                    hasActiveLimits = true;
                    const percentage = (count / limit) * 100;
                    const isWarning = percentage >= 80;
                    const isDanger = percentage >= 100;
                    
                    const color = isDanger ? '#ef4444' : isWarning ? '#f59e0b' : '#10b981';
                    const status = isDanger ? '已满' : isWarning ? '接近' : '正常';
                    
                    html += `
                        <div style="margin-bottom: 6px; padding: 6px; background: rgba(255, 255, 255, 0.1); border-radius: 4px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                                <span style="font-weight: 500;">选项 ${option}:</span>
                                <span style="color: ${color}; font-weight: 600;">${count} / ${limit}</span>
                            </div>
                            <div style="background: rgba(255, 255, 255, 0.2); height: 4px; border-radius: 2px; overflow: hidden;">
                                <div style="background: ${color}; height: 100%; width: ${Math.min(percentage, 100)}%; transition: width 0.3s ease;"></div>
                            </div>
                            <div style="font-size: 12px; color: rgba(255, 255, 255, 0.8); margin-top: 2px;">状态: ${status}</div>
                        </div>
                    `;
                }
            });

            if (hasActiveLimits) {
                this.tooltip.innerHTML = html;
                this.tooltip.style.display = 'block';
            } else {
                this.tooltip.style.display = 'none';
            }
        }
    }

    // ==================== 表格缩放功能 ====================
    class TableZoomController {
        constructor() {
            this.zoomLevel = 100;
            this.minZoom = 50;
            this.maxZoom = 200;
            this.step = 10;
            this.init();
        }

        init() {
            // 查找表格容器
            const tables = document.querySelectorAll('table');
            if (tables.length === 0) {
                return;
            }

            // 为每个表格创建缩放控制
            tables.forEach((table, index) => {
                this.wrapTable(table, index);
            });
        }

        wrapTable(table, index) {
            // 检查是否已经包装过
            if (table.parentElement.classList.contains('table-zoom-wrapper')) {
                return;
            }

            // 创建包装器
            const wrapper = document.createElement('div');
            wrapper.className = 'table-zoom-wrapper';
            wrapper.style.cssText = `
                position: relative;
                overflow: auto;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                background: white;
                margin: 20px 0;
            `;

            // 创建控制栏
            const controls = document.createElement('div');
            controls.className = 'table-zoom-controls';
            controls.style.cssText = `
                position: sticky;
                top: 0;
                background: #f9fafb;
                border-bottom: 1px solid #e5e7eb;
                padding: 8px 12px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                z-index: 10;
                font-size: 14px;
            `;

            const label = document.createElement('span');
            label.textContent = '缩放: ';
            label.style.cssText = 'margin-right: 8px; color: #6b7280; font-weight: 500;';

            const zoomDisplay = document.createElement('span');
            zoomDisplay.className = 'zoom-display';
            zoomDisplay.textContent = '100%';
            zoomDisplay.style.cssText = 'min-width: 50px; text-align: center; font-weight: 600; color: #0d9488; margin: 0 8px;';

            const zoomOutBtn = document.createElement('button');
            zoomOutBtn.textContent = '−';
            zoomOutBtn.className = 'zoom-btn zoom-out';
            zoomOutBtn.style.cssText = `
                width: 32px;
                height: 32px;
                border: 1px solid #d1d5db;
                background: white;
                border-radius: 4px;
                cursor: pointer;
                font-size: 18px;
                font-weight: 600;
                color: #374151;
                margin-right: 4px;
                transition: all 0.2s;
            `;
            zoomOutBtn.onmouseover = () => zoomOutBtn.style.background = '#f3f4f6';
            zoomOutBtn.onmouseout = () => zoomOutBtn.style.background = 'white';

            const zoomInBtn = document.createElement('button');
            zoomInBtn.textContent = '+';
            zoomInBtn.className = 'zoom-btn zoom-in';
            zoomInBtn.style.cssText = zoomOutBtn.style.cssText;
            zoomInBtn.onmouseover = () => zoomInBtn.style.background = '#f3f4f6';
            zoomInBtn.onmouseout = () => zoomInBtn.style.background = 'white';

            const resetBtn = document.createElement('button');
            resetBtn.textContent = '重置';
            resetBtn.className = 'zoom-btn zoom-reset';
            resetBtn.style.cssText = `
                margin-left: 8px;
                padding: 6px 12px;
                border: 1px solid #d1d5db;
                background: white;
                border-radius: 4px;
                cursor: pointer;
                font-size: 13px;
                color: #374151;
                transition: all 0.2s;
            `;
            resetBtn.onmouseover = () => resetBtn.style.background = '#f3f4f6';
            resetBtn.onmouseout = () => resetBtn.style.background = 'white';

            // 创建表格容器
            const tableContainer = document.createElement('div');
            tableContainer.className = 'table-zoom-container';
            tableContainer.style.cssText = `
                transform-origin: top left;
                transition: transform 0.3s ease;
                overflow: visible;
            `;

            // 绑定事件
            zoomOutBtn.onclick = () => this.zoomOut(tableContainer, zoomDisplay, zoomOutBtn, zoomInBtn);
            zoomInBtn.onclick = () => this.zoomIn(tableContainer, zoomDisplay, zoomOutBtn, zoomInBtn);
            resetBtn.onclick = () => this.resetZoom(tableContainer, zoomDisplay, zoomOutBtn, zoomInBtn);

            // 组装
            controls.appendChild(label);
            controls.appendChild(zoomOutBtn);
            controls.appendChild(zoomDisplay);
            controls.appendChild(zoomInBtn);
            controls.appendChild(resetBtn);

            wrapper.appendChild(controls);
            wrapper.appendChild(tableContainer);
            
            // 将 wrapper 插入到 table 之前
            table.parentNode.insertBefore(wrapper, table);
            // 将 table 移动到 container 内 (appendChild 会自动将元素从原位置移除)
            tableContainer.appendChild(table);

            // 初始化缩放
            this.updateZoom(tableContainer, 100, zoomDisplay, zoomOutBtn, zoomInBtn);
        }

        zoomOut(container, display, outBtn, inBtn) {
            const newZoom = Math.max(this.minZoom, this.zoomLevel - this.step);
            this.updateZoom(container, newZoom, display, outBtn, inBtn);
        }

        zoomIn(container, display, outBtn, inBtn) {
            const newZoom = Math.min(this.maxZoom, this.zoomLevel + this.step);
            this.updateZoom(container, newZoom, display, outBtn, inBtn);
        }

        resetZoom(container, display, outBtn, inBtn) {
            this.updateZoom(container, 100, display, outBtn, inBtn);
        }

        updateZoom(container, level, display, outBtn, inBtn) {
            this.zoomLevel = level;
            container.style.transform = `scale(${level / 100})`;
            display.textContent = `${level}%`;

            // 更新按钮状态
            outBtn.disabled = level <= this.minZoom;
            inBtn.disabled = level >= this.maxZoom;
            outBtn.style.opacity = level <= this.minZoom ? '0.5' : '1';
            inBtn.style.opacity = level >= this.maxZoom ? '0.5' : '1';
            outBtn.style.cursor = level <= this.minZoom ? 'not-allowed' : 'pointer';
            inBtn.style.cursor = level >= this.maxZoom ? 'not-allowed' : 'pointer';
        }
    }

    // ==================== 初始化 ====================
    function init() {
        // 等待DOM加载完成
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', init);
            return;
        }

        // 初始化选项限制跟踪
        const limitTracker = new OptionLimitTracker();

        // 初始化表格缩放
        const zoomController = new TableZoomController();

        // 监听动态添加的表格
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                mutation.addedNodes.forEach((node) => {
                    if (node.nodeType === 1) { // Element node
                        if (node.tagName === 'TABLE') {
                            zoomController.wrapTable(node, 0);
                        } else {
                            const tables = node.querySelectorAll && node.querySelectorAll('table');
                            if (tables) {
                                tables.forEach((table, index) => {
                                    zoomController.wrapTable(table, index);
                                });
                            }
                        }
                    }
                });
            });
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });
    }

    // 启动
    init();
})();
