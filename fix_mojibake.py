import os

with open('app.py', 'rb') as f:
    text_utf8 = f.read().decode('utf-8')

# Fix double-encoded UTF-8 characters
try:
    # We can try to decode by encoding to cp1252 and decoding to utf8
    # But since some parts are correct (e.g. the first m-dash might be right or wrong),
    # let's do a targeted replace for known mojibake strings
    pass
except Exception:
    pass

replacements = {
    'ðŸ“„': '📄',
    'âš™ï¸ ': '⚙️',
    'ðŸ’¾': '💾',
    'ðŸ“‹': '📋',
    'ðŸ“Š': '📊',
    'âœ‰ï¸ ': '✉️',
    'ðŸ“ ': '📝',
    'ðŸŽšï¸ ': '🎛️',
    'ðŸŽ¯': '🎯',
    'âš–ï¸ ': '⚖️',
    'âž•': '➕',
    'ðŸš€': '🚀',
    'ðŸ—„ï¸ ': '🗄️',
    'âœ…': '✅',
    'â Œ': '❌',
    'âš ï¸ ': '⚠️',
    'ðŸ¤–': '🤖',
    'â€”': '—',
    'â€œ': '“',
    'â€ ': '”',
    'â€¦': '…',
    'Â·': '·'
}

for k, v in replacements.items():
    text_utf8 = text_utf8.replace(k, v)

with open('app.py', 'wb') as f:
    f.write(text_utf8.encode('utf-8'))
print('Fixed replacements in app.py')
