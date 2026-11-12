import base64
import json
from pathlib import Path
from service import map_record


def run():
    fixture = json.loads((Path(__file__).parent / 'fixtures/rules.json').read_text())
    result = map_record(fixture['extracted'], fixture['draft_v1'])
    assert result == {'founded': '2020-01-01', 'updated': '2026-11-11'}
    responses = json.loads((Path(__file__).parent / 'fixtures/responses.json').read_text())
    raw_sizes = [len(base64.b64decode(sample['body_base64'], validate=True)) for sample in responses['samples']]
    assert all(size > 0 for size in raw_sizes)
    return {'mapped': result, 'raw_extension_candidate': fixture['extracted']['unrecognised'], 'raw_sample_sizes': raw_sizes}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
