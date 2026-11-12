import json
from pathlib import Path
from service import map_record


def run():
    fixture = json.loads((Path(__file__).parent / 'fixtures/rules.json').read_text())
    result = map_record(fixture['extracted'], fixture['draft_v1'])
    assert result == {'founded': '2020-01-01', 'updated': '2026-11-11'}
    return {'mapped': result, 'raw_extension_candidate': fixture['extracted']['unrecognised']}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
