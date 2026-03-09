import base64
import os
import re

# 图片文件列表
image_files = {
    'flat1.png': 'flat1.png',
    'flat2.png': 'flat2.png',
    'flat3.png': 'flat3.png',
    'flat4.png': 'flat4.png',
    'Minimalist1.png': 'Minimalist1.png',
    'Minimalist2.png': 'Minimalist2.png',
    'Minimalist3.png': 'Minimalist3.png',
    'Minimalist4.png': 'Minimalist4.png',
    'Tailwind1.png': 'Tailwind1.png',
    'Tailwind2.png': 'Tailwind2.png',
    'Tailwind3.png': 'Tailwind3.png',
    'Tailwind4.png': 'Tailwind4.png',
}

# 读取HTML文件
html_file = 'vote.html'
with open(html_file, 'r', encoding='utf-8') as f:
    html_content = f.read()

# 转换每张图片为base64
for img_name, img_path in image_files.items():
    if os.path.exists(img_path):
        # 读取图片文件
        with open(img_path, 'rb') as img_file:
            img_data = img_file.read()
            # 转换为base64！！！！！！！！！！！！！！！！！
            img_base64 = base64.b64encode(img_data).decode('utf-8')
            
            # 确定MIME类型（用来告诉接收方如何处理数据）
            if img_path.lower().endswith('.png'):
                mime_type = 'image/png'
            elif img_path.lower().endswith('.jpg') or img_path.lower().endswith('.jpeg'):
                mime_type = 'image/jpeg'
            else:
                mime_type = 'image/png'  # 默认
            
            # 创建data URI
            data_uri = f'data:{mime_type};base64,{img_base64}'
            
            # 替换HTML中的所有匹配项（包括各种可能的路径格式）
            patterns = [
                f'src="https://wzkjgz.site/quickform/uploads/{img_name}"',
                f'src="/quickform/uploads/{img_name}"',
                f'src="{img_name}"',
                f"src='https://wzkjgz.site/quickform/uploads/{img_name}'",
                f"src='/quickform/uploads/{img_name}'",
                f"src='{img_name}'",
            ]
            
            for pattern in patterns:
                html_content = html_content.replace(pattern, f'src="{data_uri}"')
            
            print(f'已转换: {img_name}')
    else:
        print(f'未找到: {img_path}')

# 保存修改后的HTML
output_file = 'vote.html'
with open(output_file, 'w', encoding='utf-8') as f:
    f.write(html_content)

print(f'\n完成！已更新 {output_file}')
