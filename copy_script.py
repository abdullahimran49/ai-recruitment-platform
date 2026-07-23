import os, shutil, re

src = r"D:\ATSResume"
dst = r"D:\temp_repo\Proj2_AI_Resume_Screener_Portal"

ignores_dirs = {'.git', '__pycache__', 'node_modules', '.next', 'out', 'venv', '.venv', '.pytest_cache'}
ignores_files = {'.env', 'jobs.json', 'copy_script.py'}
ignores_exts = {'.log', '.exe', '.db', '.sqlite3', '.pyc'}

email_re = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

def copy_tree(src, dst):
    os.makedirs(dst, exist_ok=True)
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            if item in ignores_dirs:
                continue
            copy_tree(s, d)
        else:
            if item in ignores_files or any(item.endswith(ext) for ext in ignores_exts):
                continue
            
            # read and sanitize if text file
            is_text = False
            if item.endswith(('.py', '.js', '.jsx', '.json', '.md', '.html', '.css', '.txt', '.example')):
                is_text = True
                
            if is_text:
                try:
                    with open(s, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # replace emails (but don't replace standard placeholders if you don't want, but let's just replace all to be safe)
                    content = email_re.sub('REDACTED@example.com', content)
                    # hide groq or openai keys
                    content = re.sub(r'(gsk_[a-zA-Z0-9]{30,})', 'gsk_REDACTED', content)
                    content = re.sub(r'(sk-[a-zA-Z0-9]{30,})', 'sk-REDACTED', content)
                    
                    with open(d, 'w', encoding='utf-8') as f:
                        f.write(content)
                except Exception as e:
                    # fallback to normal copy
                    shutil.copy2(s, d)
            else:
                shutil.copy2(s, d)

copy_tree(src, dst)
print("Done copying")
