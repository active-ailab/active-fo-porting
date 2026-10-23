#!/usr/bin/env python3
"""Extract scoped macro evidence from explicit project/core source and build inputs."""
import argparse
from pathlib import Path

from audit_common import (DEFAULT_CATALOG, assignments, catalog, config, csv_output,
                          fingerprints, identity_state, json_output, val)


def header_agrees(source_value, header_value):
    if source_value == 'UNSET':
        return 'not_in_final_config'
    if source_value == 'n':
        return header_value == 'UNSET'
    if source_value == 'm':
        return 'module_not_checked'
    expected = '1' if source_value == 'y' else source_value
    if expected == header_value:
        return True
    try:
        return int(expected, 0) == int(header_value, 0)
    except ValueError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', action='append', required=True, metavar='PROJECT.CORE=PATH')
    parser.add_argument('--final', action='append', default=[], metavar='LABEL=PATH')
    parser.add_argument('--header', action='append', default=[], metavar='LABEL=PATH')
    parser.add_argument('--expect', action='append', default=[], metavar='LABEL=PROJECT')
    parser.add_argument('--catalog', type=Path, default=DEFAULT_CATALOG)
    parser.add_argument('--features', nargs='+')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    sources, finals, headers, expected = map(assignments, [args.config, args.final, args.header, args.expect])
    if (finals.keys() | headers.keys() | expected.keys()) - sources.keys():
        parser.error('Final/header/expect labels must have a corresponding --config')
    features = catalog(args.catalog, args.features)
    events, accepted, all_paths = [], {}, [args.catalog]
    for kind, paths in [('source', sources), ('final', finals), ('header', headers)]:
        for label, path in paths.items():
            project = expected.get(label, label.rsplit('.', 1)[0])
            data = config(path, header=kind == 'header')
            state = identity_state(data, project, label)
            events.append(dict(label=label, kind=kind, path=str(Path(path).resolve()), status=state))
            all_paths.append(Path(path))
            # Sources without identity may still be inspected explicitly, but are not verified builds.
            if state.startswith('rejected'):
                if kind == 'source':
                    raise ValueError('Source identity mismatch: ' + label + ' ' + path)
                continue
            if kind == 'source' or state == 'accepted':
                accepted[(kind, label)] = data
    rows = []
    for feature_id, feature in features.items():
        for label in sources:
            src = accepted[('source', label)]
            final = accepted.get(('final', label))
            header = accepted.get(('header', label))
            for macro in feature.get('macros', []):
                sv = val(src, macro)
                fv = val(final, macro) if final is not None else 'NOT_VERIFIED'
                hv = val(header, macro) if header is not None else 'NOT_VERIFIED'
                rows.append(dict(feature=feature_id, feature_name=feature['name'], label=label, macro=macro,
                    source_value=sv, source_line=src.get(macro, {}).get('line', ''),
                    final_value=fv, final_line=(final or {}).get(macro, {}).get('line', ''),
                    header_value=hv, header_line=(header or {}).get(macro, {}).get('line', ''),
                    source_final_equal=(sv == fv) if final is not None else 'NOT_VERIFIED',
                    final_header_consistent=header_agrees(fv, hv) if final is not None and header is not None else 'NOT_VERIFIED'))
    comparisons = []
    for feature_id, feature in features.items():
        for macro in feature.get('macros', []):
            for core in sorted({label.rsplit('.', 1)[-1] for label in sources}):
                values = {r['label']: r['source_value'] for r in rows
                          if r['feature'] == feature_id and r['macro'] == macro and r['label'].endswith('.' + core)}
                if len(values) > 1:
                    comparisons.append(dict(feature=feature_id, macro=macro, core=core,
                                            values=values, same=len(set(values.values())) == 1))
    args.out.mkdir(parents=True, exist_ok=True)
    csv_output(args.out / 'macros.csv', rows, ['feature','feature_name','label','macro','source_value',
        'source_line','final_value','final_line','header_value','header_line','source_final_equal','final_header_consistent'])
    summary = dict(scope=list(features), evidence_events=events, source_comparisons=comparisons,
        differences=[r for r in comparisons if not r['same']],
        note='UNSET is not n; equal source values are not a functional pass. Rejected/unverified build inputs are excluded.',
        files=fingerprints(all_paths))
    json_output(args.out / 'macros.json', summary)
    print(f'Macro rows: {len(rows)}; cross-project differences: {len(summary["differences"])}; evidence events: {len(events)}')


if __name__ == '__main__':
    main()
