"""Allowlist-only export following dataset-plan.md; no backend interface exists yet."""
import argparse
import json
from pathlib import Path
from validate import read_jsonl, validate_record


def input_only(record, after=None):
    validate_record(record)
    utterances = record['utterances']
    if after is not None:
        ids = [u['utterance_id'] for u in utterances]
        if after not in ids:
            raise ValueError('Unknown checkpoint utterance')
        utterances = utterances[:ids.index(after)+1]
    return {'transcript_id': record['transcript_id'], 'utterances': [
        {k: u[k] for k in ('utterance_id', 'sequence', 'speaker_id', 'text')} for u in utterances]}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path)
    p.add_argument('destination', type=Path, help='write outside dataset files, preferably a temporary directory')
    p.add_argument('--after', help='optional prefix ending at this utterance ID')
    args = p.parse_args()
    if args.destination.resolve().parent == Path(__file__).resolve().parent or args.destination.resolve() == args.source.resolve():
        p.error('Choose an output outside the dataset folder; gold files must not be overwritten.')
    inputs = [input_only(r, args.after) for r in read_jsonl(args.source)]
    args.destination.write_text(''.join(json.dumps(x, ensure_ascii=False)+'\n' for x in inputs), encoding='utf-8')
    print(f'Exported {len(inputs)} input-only records. Keep evaluation exports in the evaluation harness.')
