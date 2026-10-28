"""Edit only this relay's TOML entries; Python 3.9+, no dependencies."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

try:
    import tomllib
except ImportError:  # The upstream installer also supports Python 3.9/3.10.
    tomllib = None

PROVIDER = "openai-retry"
ROOT = 'model_provider = "openai-retry"'
BLOCK = '''[model_providers.openai-retry]
name = "OpenAI (retry)"
base_url = "http://127.0.0.1:8787"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 20
request_max_retries = 10
'''
KEY = re.compile(r'''\s*("(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_-]+)\s*''')


def key_path(value):
    """Decode table/key paths, including quoted keys; reject ambiguous syntax."""
    parts = []
    while value:
        match = KEY.match(value)
        if not match:
            raise ValueError("Unsupported TOML key syntax; config was not changed")
        token = match[1]
        parts.append(json.loads(token) if token.startswith('"') else token.strip("'"))
        value = value[match.end():]
        if not value:
            break
        if not value.startswith('.'):
            raise ValueError("Unsupported TOML key syntax; config was not changed")
        value = value[1:]
    return parts


def statements(text):
    """Find statement boundaries without confusing strings/arrays with TOML tables."""
    start = 0
    quote = None
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if quote.startswith('"') and char == '\\':
                index += 2
                continue
            if text.startswith(quote, index):
                index += len(quote)
                quote = None
                continue
        elif char == '#':
            end = text.find('\n', index)
            index = len(text) if end < 0 else end
            continue
        elif char in ('"', "'"):
            quote = char * 3 if text.startswith(char * 3, index) else char
            index += len(quote)
            continue
        elif char in '[{':
            depth += 1
        elif char in ']}':
            depth -= 1
        if char == '\n' and not quote and depth == 0:
            yield start, index + 1, text[start:index + 1]
            start = index + 1
        index += 1
    if quote or depth:
        raise ValueError("Unterminated TOML string or collection; config was not changed")
    if start < len(text):
        yield start, len(text), text[start:]


def locations(text):
    root = None
    provider = None
    headers = []
    section = []
    for start, end, raw in statements(text):
        value = raw.strip()
        if not value or value.startswith('#'):
            continue
        if value.startswith('['):
            header = re.match(r'^\[(\[?)(.*?)\]\]?\s*(?:#.*)?$', value, re.S)
            if not header:
                raise ValueError("Unsupported TOML table header")
            section = key_path(header[2].strip())
            headers.append((start, section))
            if section[:2] == ['model_providers', PROVIDER]:
                if header[1] or len(section) != 2 or provider is not None:
                    raise ValueError("Nested or duplicate relay tables require manual editing")
                provider = (start, len(text))
        else:
            # The equals sign in a quoted key is irrelevant to our simple managed keys.
            match = re.match(r'''^((?:"(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_. -])+?)\s*=''', value)
            if not match:
                raise ValueError("Unsupported TOML assignment; config was not changed")
            path = section + key_path(match[1].strip())
            if path == ['model_provider']:
                if root is not None:
                    raise ValueError("Duplicate model_provider")
                root = (start, end)
            if path[:2] == ['model_providers', PROVIDER] and section != path[:2]:
                raise ValueError("Inline/dotted relay settings require manual editing")
    if provider:
        provider = (provider[0], next((start for start, _ in headers if start > provider[0]), len(text)))
    return root, provider


def piece(text, span):
    return text[span[0]:span[1]] if span else None


def same_block(actual, expected):
    return actual == expected or (actual is not None and expected is not None and actual.rstrip() == expected.rstrip())


def replace(text, root, block):
    root_span, block_span = locations(text)
    edits = []
    if root_span:
        edits.append((*root_span, root or ''))
    if block_span:
        edits.append((*block_span, block or ''))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    if not root_span and root:
        text = root + text
    if not block_span and block:
        text += ('\n' if text and not text.endswith('\n\n') else '') + block
    return text


def read(path):
    return path.read_bytes() if path.exists() else b''


def decode(data):
    return data.decode('utf-8-sig')


def encode(text, original):
    return (b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')


def atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def config_lock(path):
    with path.open('x'):
        pass
    try:
        yield
    finally:
        path.unlink()


def apply(action, path):
    path = path.resolve()
    state_path = path.with_name(path.name + '.overload-fix.json')
    if action == 'remove' and not path.exists() and not state_path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.overload-fix.lock')
    # Exclusive lock: another run must finish before this one starts.
    with config_lock(lock):
        current = read(path)
        text = decode(current)
        if tomllib:
            tomllib.loads(text)
        root_span, block_span = locations(text)
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else None
        if state and state['config_path'] != str(path):
            raise ValueError("Backup belongs to another config path")
        newline = '\r\n' if '\r\n' in text else '\n'
        expected_root = ROOT + newline
        expected_block = BLOCK.replace('\n', newline)
        if action in ('check', 'install'):
            if state:
                if piece(text, root_span) != state['installed_root'] or not same_block(piece(text, block_span), state['installed_block']):
                    raise ValueError("Relay entries changed since installation; remove or reconcile them first")
                print("Relay configuration is already installed.")
                return
            updated = replace(text, expected_root, expected_block)
        else:
            if state:
                # Preserve a later provider selection, but never overwrite an edited relay table.
                actual_block = piece(text, block_span)
                if not any(same_block(actual_block, candidate) for candidate in (state['installed_block'], state['original_block'])):
                    raise ValueError("Relay table changed since installation; review it before removal")
                root = piece(text, root_span)
                updated = replace(text, state['original_root'] if root == state['installed_root'] else root, state['original_block'])
                if hashlib.sha256(current).hexdigest() == state['installed_hash']:
                    original = Path(state['backup']).read_bytes()
                    if hashlib.sha256(original).hexdigest() != state['original_hash']:
                        raise ValueError("Backup checksum mismatch")
                    updated = decode(original)
            else:
                # Also undo the exact manual snippet documented by upstream.
                root = piece(text, root_span)
                remove_root = bool(root and re.fullmatch(r'''\s*model_provider\s*=\s*["']openai-retry["']\s*(?:#.*)?''', root.strip()))
                if block_span and not same_block(piece(text, block_span), expected_block):
                    if remove_root:
                        raise ValueError("Untracked relay table differs from defaults; review it before removal")
                    return  # Already restored, or belongs to an unrelated setup.
                if root and PROVIDER in root and not remove_root:
                    raise ValueError("Unrecognized provider selection; review it before removal")
                updated = replace(text, None if remove_root else root, None)
        if tomllib:
            tomllib.loads(updated)
        if action == 'check':
            print("Configuration preflight passed: " + str(path))
            return
        output = encode(updated, current)
        if action == 'install':
            backup = path.with_name(path.name + '.overload-fix.' + uuid.uuid4().hex + '.bak')
            atomic_write(backup, current)
            state = dict(config_path=str(path), backup=str(backup), original_root=piece(text, root_span),
                         original_block=piece(text, block_span), installed_root=expected_root,
                         installed_block=expected_block, original_hash=hashlib.sha256(current).hexdigest(),
                         installed_hash=hashlib.sha256(output).hexdigest())
            atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8'))
        if current != output:
            if read(path) != current:
                raise ValueError("Config changed during this operation; nothing was overwritten")
            if action == 'remove':
                backup = path.with_name(path.name + '.before-remove.' + uuid.uuid4().hex + '.bak')
                atomic_write(backup, current)
            atomic_write(path, output)
        if action == 'remove' and state_path.exists():
            state_path.unlink()
        print("Configuration " + ('installed: ' if action == 'install' else 'removed: ') + str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'install', 'remove'))
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    path = args.config or Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'
    try:
        apply(args.action, path)
    except (OSError, ValueError, KeyError) as error:
        print('Configuration failed: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
