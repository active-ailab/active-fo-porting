"""Shared read-only audit extraction; outputs are evidence, not product verdicts."""
import csv
import hashlib
import json
from pathlib import Path
import re

DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / 'references/owned-features.yaml'


def catalog(path, selected):
    import yaml
    features = yaml.safe_load(Path(path).read_text(encoding='utf-8'))['features']
    unknown = set(selected or []) - features.keys()
    if unknown:
        raise ValueError('Unknown features: ' + ', '.join(sorted(unknown)))
    return {k: v for k, v in features.items() if not selected or k in selected}


def assignments(values):
    result = {}
    for value in values:
        label, sep, path = value.partition('=')
        if not sep or not label or not path or label in result:
            raise ValueError('Expected unique LABEL=VALUE: ' + value)
        result[label] = path
    return result


def config(path, header=False):
    result = {}
    pattern = r'#define\s+(\w+)\s+(.+)' if header else r'(\w+)=(.*)'
    for number, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        match = re.fullmatch(pattern, line.strip())
        if match:
            result[match[1]] = {'value': match[2], 'line': number}
        if not header:
            match = re.fullmatch(r'# (\w+) is not set', line.strip())
            if match:
                result[match[1]] = {'value': 'n', 'line': number}
    if not result:
        raise ValueError('No configuration parsed: ' + str(path))
    return result


def val(data, name):
    return data.get(name, {}).get('value', 'UNSET')


def identity(data):
    values = [val(data, name).strip('"').lower() for name in
              ['HMI_PRODUCT_CUSTOMIZE_DIR', 'BUILD_PROJECT_PATH_NAME'] if name in data]
    return sorted(set(values))


def identity_state(data, expected, label):
    ids = identity(data)
    if ids and ids != [expected.lower()]:
        return 'rejected_project_mismatch'
    if not ids:
        return 'unverified_project_identity'
    role = label.rsplit('.', 1)[-1]
    hub = val(data, 'APP_SENSORHUB')
    if role == 'hub' and hub not in ['y', '1']:
        return 'unverified_core_role' if hub == 'UNSET' else 'rejected_core_mismatch'
    if role == 'main' and hub in ['y', '1']:
        return 'rejected_core_mismatch'
    return 'accepted'


def csv_output(path, rows, columns):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def json_output(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def fingerprints(paths):
    return [{'path': str(p.resolve()), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(set(map(Path, paths)))]


def bool_entries(path):
    result = []
    for number, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        match = re.search(r'\b(true|false)\s*,?\s*/\*\s*(.*?)\s*\*/', line)
        if match:
            result.append((match[1] == 'true', number, match[2]))
    if not result:
        raise ValueError('No generated boolean entries: ' + str(path))
    return result
