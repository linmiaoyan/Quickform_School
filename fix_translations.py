import sys
import re
import ast

file_path = r'D:\OneDrive\09教育技术处\QuickForm\core\i18n.py'
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

match = re.search(r'TRANSLATIONS\s*=\s*(\{.*?\})\n\n\ndef get_locale', content, re.DOTALL)
if not match:
    print('Could not find TRANSLATIONS dict')
    sys.exit(1)

dict_str = match.group(1)
trans = ast.literal_eval(dict_str)

zh_keys = trans['zh-simple']
tw_keys = trans['zh-TW']
en_keys = trans['en']

all_keys = set(zh_keys.keys()) | set(tw_keys.keys()) | set(en_keys.keys())

missing_in_zh = sorted(list(all_keys - set(zh_keys.keys())))
missing_in_tw = sorted(list(all_keys - set(tw_keys.keys())))
missing_in_en = sorted(list(all_keys - set(en_keys.keys())))

def fill_missing(lang_dict, missing_keys, target_lang):
    added_str = ''
    for k in missing_keys:
        if target_lang == 'zh-simple':
            val = tw_keys.get(k, en_keys.get(k, k))
        elif target_lang == 'zh-TW':
            val = zh_keys.get(k, en_keys.get(k, k))
        elif target_lang == 'en':
            val = zh_keys.get(k, tw_keys.get(k, k))
            
        val = val.replace("'", "\\'")
        added_str += f"        '{k}': '{val}',\n"
    return added_str

zh_add = fill_missing(zh_keys, missing_in_zh, 'zh-simple')
tw_add = fill_missing(tw_keys, missing_in_tw, 'zh-TW')
en_add = fill_missing(en_keys, missing_in_en, 'en')

# Now insert them into the content
# We will find the end of each dict block
# end of zh-simple
content = re.sub(r"(    'zh-simple': \{.*?)(    \},?\n\s*'zh-TW': \{)", lambda m: m.group(1) + "        # --- Auto added ---\n" + zh_add + m.group(2), content, flags=re.DOTALL)

# end of zh-TW
content = re.sub(r"(    'zh-TW': \{.*?)(    \},?\n\s*'en': \{)", lambda m: m.group(1) + "        # --- Auto added ---\n" + tw_add + m.group(2), content, flags=re.DOTALL)

# end of en
content = re.sub(r"(    'en': \{.*?)(\n    \}\n\})", lambda m: m.group(1) + "\n        # --- Auto added ---\n" + en_add + m.group(2), content, flags=re.DOTALL)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"Added {len(missing_in_zh)} keys to zh-simple")
print(f"Added {len(missing_in_tw)} keys to zh-TW")
print(f"Added {len(missing_in_en)} keys to en")
