import argparse
import json
from pathlib import Path
from . import health, care, handoff, medical_claim, insurer_import
from .report import dumps, report
from .source import file_record
from .store import connect


def main(argv=None):
    parser = argparse.ArgumentParser(prog='petboard')
    parser.add_argument('command', choices=['intake-import', 'care-import', 'handoff-import', 'claim-import',
                                            'insurer-import', 'report', 'audit', 'demo'])
    parser.add_argument('--db', required=True)
    parser.add_argument('--file')
    args = parser.parse_args(argv)
    db = connect(args.db)
    try:
        if args.command == 'report':
            print(dumps(db))
            return
        if args.command == 'audit':
            print(json.dumps(audit(db), ensure_ascii=False, indent=2))
            return
        if args.command == 'demo':
            root = Path(__file__).parent.parent / 'fixtures'
            for command, filename in [('intake-import', 'intake_ok.json'), ('care-import', 'care_ok.json'),
                                      ('handoff-import', 'handoff_ok.json')]:
                run_import(db, command, (root / filename).read_bytes())
            print(dumps(db))
            return
        if not args.file:
            parser.error('--file is required')
        print(json.dumps(run_import(db, args.command, Path(args.file).read_bytes()), ensure_ascii=False, indent=2))
    finally:
        db.close()


def run_import(db, command, raw):
    data = json.loads(raw)
    file_record(db, command, raw)
    importer = {'intake-import': health.import_data, 'care-import': care.import_data,
                'handoff-import': handoff.import_data, 'claim-import': medical_claim.import_data,
                'insurer-import': insurer_import.import_data}[command]
    return importer(db, data)


def audit(db):
    replay = connect(':memory:')
    try:
        source_files = db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for item in source_files:
            run_import(replay, item['kind'], item['raw'])
        original = report(db)
        restored = report(replay)
        if original != restored:
            raise ValueError('replayed source files disagree with persisted decisions')
        return {'status': 'matched', 'source_files': len(source_files), 'animals': len(original['animals'])}
    finally:
        replay.close()


if __name__ == '__main__':
    main()
