import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

LINK = re.compile(r"(?P<head>\]\()(?P<url>[^\s)]+)(?P<tail>\))")
RECORD = re.compile(r"^(\d{7})(?:\.(?:xml|md)|_links(?:/.*)?)$")


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def remap(rel):
    if not rel.startswith('records/'):
        return rel
    rest = rel[len('records/'):]
    match = RECORD.fullmatch(rest)
    if not match:
        return rel
    bucket = (int(match.group(1)) - 1) // 1000
    return f'records/{bucket:03d}/{rest}'


def local_target(old_file, destination):
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    if parsed.path.startswith('/') or '\\' in parsed.path:
        raise ValueError(f'Unexpected absolute or Windows link: {old_file}: {destination}')
    target = posixpath.normpath(posixpath.join(posixpath.dirname(old_file), unquote(parsed.path)))
    if target == '..' or target.startswith('../'):
        raise ValueError(f'Link leaves navigation tree: {old_file}: {destination}')
    return parsed, target


def rewrite_markdown(text, old_file, new_file):
    def replace(match):
        info = local_target(old_file, match.group('url'))
        if info is None:
            return match.group(0)
        parsed, target = info
        destination = posixpath.relpath(remap(target), posixpath.dirname(new_file) or '.')
        destination = quote(destination, safe='/._~-')
        if parsed.query:
            destination += '?' + parsed.query
        if parsed.fragment:
            destination += '#' + parsed.fragment
        return match.group('head') + destination + match.group('tail')
    return LINK.sub(replace, text)


def rewrite_json_values(value):
    if isinstance(value, dict):
        return {k: rewrite_json_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite_json_values(v) for v in value]
    if isinstance(value, str) and value.startswith('records/'):
        return remap(value)
    return value


def main():
    parser = argparse.ArgumentParser(description='Reorganize existing version 2.0 navigation, without re-extracting XML.')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    source, target = args.source.resolve(), args.out.resolve()
    stage = target.with_name(target.name + '.building')
    if not source.is_dir():
        raise ValueError('Source directory not found')
    if target == source or source in target.parents or target in source.parents:
        raise ValueError('Source and output must be separate, non-nested directories')
    if target.exists() or stage.exists():
        raise ValueError('Output or .building already exists; nothing overwritten')
    if (source / 'INCOMPLETE.txt').exists():
        raise ValueError('Source contains INCOMPLETE.txt')
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8-sig'))
    if manifest.get('status') not in ('BUILT', 'BUILT_WITH_WARNINGS'):
        raise ValueError('Source build status is not completed')
    if not manifest.get('index_matches'):
        raise ValueError('Source does not confirm input index hash')
    if manifest.get('omitted_top_level_elements') != 0:
        raise ValueError('Source reports omitted records')
    print('1/4: Enumerating source files...', flush=True)
    entries = []
    destinations = set()
    for root, dirs, names in os.walk(source):
        for name in dirs + names:
            if (Path(root) / name).is_symlink():
                raise ValueError('Symbolic links are not supported')
        for name in names:
            path = Path(root) / name
            rel = path.relative_to(source).as_posix()
            if rel == 'files.sha256':
                continue
            new = remap(rel)
            if new in destinations:
                raise ValueError(f'Destination collision: {new}')
            destinations.add(new)
            entries.append((rel, new))
    entries.sort()
    xml_total = sum(rel.startswith('records/') and rel.endswith('.xml') for rel, _ in entries)
    if xml_total != manifest.get('exported_xml_elements'):
        raise ValueError('XML count does not match source manifest')
    expected_hashes = {}
    for line in (source / 'files.sha256').read_text(encoding='utf-8').splitlines():
        h, separator, name = line.partition('  ')
        if not separator or not re.fullmatch('[0-9a-f]{64}', h):
            raise ValueError('Unexpected files.sha256 format')
        expected_hashes[name] = h
    if set(expected_hashes) != {rel for rel, _ in entries}:
        raise ValueError('Source checksum file does not match source file list')
    print(f'Files: {len(entries)}; XML: {xml_total}; source unchanged.', flush=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage.mkdir()
    hashes = {}
    broken = []
    checked_links = 0
    started = time.monotonic()
    try:
        print('2/4: Copying, verifying source hashes and rewriting links...', flush=True)
        for number, (rel, new) in enumerate(entries, 1):
            src, dst = source / rel, stage / new
            data = src.read_bytes()
            old_hash = hashlib.sha256(data).hexdigest()
            if old_hash != expected_hashes[rel]:
                raise ValueError(f'Source hash mismatch: {rel}')
            dst.parent.mkdir(parents=True, exist_ok=True)
            if rel.endswith('.md'):
                text = rewrite_markdown(data.decode('utf-8'), rel, new)
                data = text.encode('utf-8')
                for match in LINK.finditer(text):
                    info = local_target(new, match.group('url'))
                    if info is not None:
                        checked_links += 1
                        linked = info[1]
                        if linked not in destinations and not any_name_is_directory(linked, destinations):
                            if len(broken) < 100:
                                broken.append({'file': new, 'target': linked})
            elif rel == 'manifest.json':
                result = rewrite_json_values(manifest)
                result['navigation_layout'] = {
                    'version': 'sharded-1', 'records_per_bucket': 1000,
                    'tool': 'repack_structure_navigation.py',
                    'source_manifest_sha256': old_hash,
                    'note': 'XML bytes preserved; relative Markdown links rewritten. Original build warnings retained.'
                }
                data = (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            dst.write_bytes(data)
            wanted = hashlib.sha256(data).hexdigest()
            if digest(dst) != wanted:
                raise ValueError(f'Output verification failed: {new}')
            hashes[new] = wanted
            if number % 1000 == 0 or number == len(entries):
                print(f'{number}/{len(entries)} files; {time.monotonic()-started:.0f} s', flush=True)
        if broken:
            (stage / 'BROKEN_LINKS.json').write_text(json.dumps(broken, ensure_ascii=False, indent=2), encoding='utf-8')
            raise ValueError('Broken local Markdown links detected; see BROKEN_LINKS.json')
        print('3/4: Writing checksum list and relocation report...', flush=True)
        report = {
            'status': 'REPACKED', 'files_from_source': len(entries),
            'xml_records': xml_total, 'xml_bytes_preserved': True,
            'local_markdown_links_checked': checked_links,
            'source_sha256': manifest.get('source_sha256'),
            'source_build_status': manifest.get('status'),
            'warnings': manifest.get('warnings', []),
            'limits': 'Checks link paths, not anchor existence or business completeness. No 1C execution.'
        }
        report_path = stage / 'REPACK_REPORT.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        hashes['REPACK_REPORT.json'] = digest(report_path)
        with (stage / 'files.sha256').open('w', encoding='utf-8', newline='\n') as f:
            for name in sorted(hashes):
                f.write(hashes[name] + '  ' + name + '\n')
        print('4/4: Finalizing output...', flush=True)
        if target.exists():
            raise ValueError('Output appeared while processing; refusing overwrite')
        stage.rename(target)
        print(json.dumps({'status': 'REPACKED', 'output': str(target), 'xml': xml_total,
                          'links_checked': checked_links}, ensure_ascii=False, indent=2), flush=True)
    except BaseException:
        if stage.exists():
            (stage / 'INCOMPLETE.txt').write_text('Repacking did not complete. Do not publish this directory. Source was not changed.\n', encoding='utf-8')
        raise


def any_name_is_directory(name, names):
    # Directory links are uncommon in this generator; avoid scanning for file links.
    if Path(name).suffix:
        return False
    prefix = name.rstrip('/') + '/'
    return any(item.startswith(prefix) for item in names)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('INTERRUPTED: source unchanged; incomplete output must not be published')
    except Exception as exc:
        raise SystemExit(f'FAILED: {type(exc).__name__}: {exc}')
