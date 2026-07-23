import re

filepath = r'D:\ATSResume\portal\frontend\app\admin\dashboard\page.jsx'

with open(filepath, 'r', encoding='utf-8') as f:
    text = f.read()

# Replace time parsing
text = re.sub(
    r'new Date\((c\.expires_at|iv\.scheduled_at|e\.at|aiDetail\.scheduled_at)\)',
    r"new Date(\1 + (\1.includes('Z') ? '' : 'Z'))",
    text
)

old_buttons = '''                          <button className="secondary" style={{ marginTop: 0, marginRight: 4, padding: "2px 8px", fontSize: "0.8em" }}
                                  onClick={() => setShowInterview(c.uuid)}
                                  title="Schedule interview">🗓️</button>
                          <button className="secondary" style={{ marginTop: 0, padding: "2px 8px", fontSize: "0.8em" }}
                                  onClick={() => setShowAIForm(c.uuid)}
                                  title="Schedule automated AI voice interview">🤖</button>'''

new_buttons = '''                          <button className="secondary" style={{ marginTop: 0, marginRight: 4, padding: "2px 8px", fontSize: "0.8em" }}
                                  onClick={() => setShowInterview(c.uuid)}
                                  title="Schedule standard interview (Human)">🗓️ Human Interview</button>
                          <button className="secondary" style={{ marginTop: 0, padding: "2px 8px", fontSize: "0.8em" }}
                                  onClick={() => setShowAIForm(c.uuid)}
                                  title="Schedule automated AI voice interview">🤖 AI Interview</button>'''

if old_buttons in text:
    text = text.replace(old_buttons, new_buttons)
else:
    print('Warning: Could not find old buttons to replace')

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(text)

print('Patched page.jsx successfully')
