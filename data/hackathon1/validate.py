"""Offline structural validation; annotations and source authenticity also need review."""
import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LABELS = {'concerning', 'benign', 'ambiguous'}
CONCERNS = {'dismissal', 'exclusion', 'gender_stereotyping', 'belittling', 'pressure_on_choice'}
DECISIONS = {'suggest', 'no_nudge', 'needs_context'}
COUNTS = {'development': {'concerning': 4, 'benign': 3, 'ambiguous': 3},
          'evaluation': {'concerning': 2, 'benign': 2, 'ambiguous': 1}}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def fields(obj, spec, where):
    require(type(obj) is dict, f'{where}: expected object')
    require(set(obj) == set(spec), f'{where}: missing/extra fields {set(obj) ^ set(spec)}')
    for key, types in spec.items():
        types = types if isinstance(types, tuple) else (types,)
        require(type(obj[key]) in types, f'{where}.{key}: wrong type')
        if type(obj[key]) is str:
            require(bool(obj[key].strip()), f'{where}.{key}: empty string')


def read_jsonl(path):
    raw = Path(path).read_bytes()
    require(not raw.startswith(b'\xef\xbb\xbf'), f'{path}: unexpected BOM')
    text = raw.decode('utf-8', errors='strict')
    require(text.endswith('\n'), f'{path}: missing final newline')
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f'{path}: duplicate JSON key {key}')
            result[key] = value
        return result
    records = []
    for i, line in enumerate(text.splitlines(), 1):
        require(bool(line.strip()), f'{path}:{i}: blank line')
        records.append(json.loads(line, object_pairs_hook=unique_keys,
                                  parse_constant=lambda x: require(False, f'non-finite {x}')))
    return records


def category(labels):
    return next((x for x in ('concerning', 'ambiguous') if x in labels), 'benign')


def validate_record(r):
    fields(r, dict(transcript_id=str, split=str, origin=str, speaker_mapping=str,
                   language=str, sources=list, utterances=list, annotations=list, review=dict), 'conversation')
    cid = r['transcript_id']
    require(re.fullmatch(r'C00[1-9]|C01[0-5]', cid), 'invalid transcript ID')
    require(r['split'] in COUNTS and r['language'] == 'en', f'{cid}: split/language')
    require(r['origin'] in {'source_excerpt', 'adapted', 'synthetic'}, f'{cid}: origin')
    require(r['speaker_mapping'] in {'observed', 'constructed'}, f'{cid}: speaker mapping')
    fields(r['review'], dict(status=str, notes=str), f'{cid}.review')
    require(r['review']['status'] in {'pending', 'checked'}, f'{cid}: review status')
    require(2 <= len(r['utterances']) <= 8, f'{cid}: turn count')
    for i, u in enumerate(r['utterances'], 1):
        fields(u, dict(utterance_id=str, sequence=int, speaker_id=str, text=str), f'{cid}.utterance')
        require(u['utterance_id'] == f'U{i}' and u['sequence'] == i, f'{cid}: IDs/order')
        require(re.fullmatch(r'P[1-4]|F1', u['speaker_id']), f'{cid}: speaker ID')
        require(not any(c in u['text'] for c in '\n\r\x00'), f'{cid}: text control characters')
        require(not re.search(r'https?://|[\w.+-]+@[\w.-]+\.[a-z]{2,}', u['text'], re.I), f'{cid}: potential PII/link')
    speakers = {u['speaker_id'] for u in r['utterances']}
    require(2 <= len(speakers) <= 4, f'{cid}: speaker count')
    participant_numbers = sorted(int(s[1:]) for s in speakers if s.startswith('P'))
    require(participant_numbers == list(range(1, len(participant_numbers)+1)), f'{cid}: skipped speaker IDs')
    ids = [u['utterance_id'] for u in r['utterances']]
    require(bool(r['annotations']), f'{cid}: no checkpoints')
    positions = []
    for cp in r['annotations']:
        fields(cp, dict(analysis_after_utterance_id=str, scenario_category=str, utterance_labels=list,
                        expected_decision=str, decision_rationale=str, example_nudge=(str, type(None))), f'{cid}.checkpoint')
        require(cp['analysis_after_utterance_id'] in ids, f'{cid}: checkpoint reference')
        n = ids.index(cp['analysis_after_utterance_id']) + 1
        positions.append(n)
        require([x.get('utterance_id') for x in cp['utterance_labels']] == ids[:n], f'{cid}: visible labels')
        for u, label in zip(r['utterances'][:n], cp['utterance_labels']):
            fields(label, dict(utterance_id=str, label=str, concern_types=list, evidence_excerpts=list, rationale=str), f'{cid}.label')
            require(label['label'] in LABELS, f'{cid}: label enum')
            require(all(type(x) is str and x in CONCERNS for x in label['concern_types']), f'{cid}: concern enum')
            require(len(set(label['concern_types'])) == len(label['concern_types']), f'{cid}: repeated concern')
            require(all(type(x) is str and x and x in u['text'] for x in label['evidence_excerpts']), f'{cid}: exact evidence')
            if label['label'] == 'concerning':
                require(bool(label['evidence_excerpts']) and bool(label['concern_types']), f'{cid}: concerning evidence/type')
            if label['label'] == 'benign':
                require(not label['concern_types'], f'{cid}: benign concerns')
        require(cp['scenario_category'] == category([x['label'] for x in cp['utterance_labels']]), f'{cid}: category precedence')
        require(cp['expected_decision'] in DECISIONS, f'{cid}: decision enum')
        require((cp['example_nudge'] is not None) == (cp['expected_decision'] == 'suggest'), f'{cid}: nudge nullability')
        if cp['example_nudge'] is not None:
            require(bool(cp['example_nudge'].strip()) and len(cp['example_nudge'].split()) <= 35, f'{cid}: nudge length')
    require(positions == sorted(set(positions)) and positions[-1] == len(ids), f'{cid}: final/ordered checkpoints')
    require(bool(r['sources']) == (r['origin'] != 'synthetic'), f'{cid}: origin/source mismatch')
    if r['origin'] == 'synthetic':
        require(r['speaker_mapping'] == 'constructed' and 'coverage gap' in r['review']['notes'].lower(), f'{cid}: synthetic justification')
    mapped = set()
    added = set()
    for s in r['sources']:
        fields(s, dict(dataset=str, version=(str, type(None)), record_id=str, source_split=(str, type(None)),
                       url=str, license=str, utterance_ids=list, changes=dict, source_labels=dict), f'{cid}.source')
        require(s['dataset'] in {'Social Bias Frames', 'Civil Comments', 'AMI Meeting Corpus'}, f'{cid}: source dataset')
        require(s['url'].startswith('https://') and s['license'] in {'CC-BY-4.0', 'CC0-1.0'}, f'{cid}: source URL/license')
        require(bool(s['utterance_ids']) and len(set(s['utterance_ids'])) == len(s['utterance_ids']) and set(s['utterance_ids']) <= set(ids), f'{cid}: source mappings')
        fields(s['changes'], dict(copied_utterances=list, adapted_utterances=list, added_utterances=list,
                                 description=str, speaker_map=dict), f'{cid}.changes')
        ch = s['changes']
        for k in ('copied_utterances', 'adapted_utterances', 'added_utterances'):
            require(all(type(x) is str and x in ids for x in ch[k]) and len(ch[k]) == len(set(ch[k])), f'{cid}: changes IDs')
        require(set(ch['copied_utterances']).isdisjoint(ch['adapted_utterances']), f'{cid}: copy/adapt overlap')
        require(set(ch['copied_utterances'] + ch['adapted_utterances']) == set(s['utterance_ids']), f'{cid}: adaptation mapping')
        require(set(ch['added_utterances']).isdisjoint(s['utterance_ids']), f'{cid}: added/source overlap')
        mapped.update(s['utterance_ids']); added.update(ch['added_utterances'])
        if s['dataset'] != 'AMI Meeting Corpus':
            require(r['speaker_mapping'] == 'constructed' and not ch['speaker_map'], f'{cid}: invented post speaker identity')
        else:
            require(r['speaker_mapping'] == 'observed' and set(ch['speaker_map'].values()) == speakers
                    and len(set(ch['speaker_map'].values())) == len(ch['speaker_map']), f'{cid}: AMI speaker relationships')
        if r['origin'] == 'source_excerpt':
            require(not ch['adapted_utterances'] and not ch['added_utterances'], f'{cid}: excerpt rewrites')
        if r['origin'] == 'adapted':
            require(bool(ch['adapted_utterances'] or ch['added_utterances']), f'{cid}: missing adaptation metadata')
    if r['sources']:
        require(mapped.isdisjoint(added) and mapped | added == set(ids), f'{cid}: unmapped turns')


def normalized(text):
    return ' '.join(re.findall(r'\w+', text.casefold()))


def duplicate_pairs(records):
    pairs = []
    for i, a in enumerate(records):
        ta = normalized(' '.join(u['text'] for u in a['utterances']))
        for b in records[i+1:]:
            tb = normalized(' '.join(u['text'] for u in b['utterances']))
            score = SequenceMatcher(None, ta, tb, autojunk=False).ratio()
            wa, wb = set(ta.split()), set(tb.split())
            jaccard = len(wa & wb) / len(wa | wb)
            if score >= .82 or jaccard >= .75:
                pairs.append((a['transcript_id'], b['transcript_id'], round(score, 3), round(jaccard, 3)))
            for ua in a['utterances']:
                for ub in b['utterances']:
                    x, y = normalized(ua['text']), normalized(ub['text'])
                    if min(len(x.split()), len(y.split())) >= 8 and SequenceMatcher(None, x, y, autojunk=False).ratio() >= .9:
                        pairs.append((a['transcript_id'], b['transcript_id'], 'similar substantive turn'))
    return pairs


def read_manifest(root=ROOT):
    with (root / 'provenance.csv').open(encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for k in ('source_split', 'version', 'original_text', 'source_labels', 'utterance_ids', 'changes'):
            row[k] = json.loads(row[k])
    return rows


def validate_manifest(records, rows, complete=True):
    require(len(rows) == 30, 'candidate shortlist must contain 30 records')
    require(len({x['candidate_id'] for x in rows}) == len(rows), 'candidate IDs duplicated')
    keys = [(x['dataset'], x['record_id']) for x in rows]
    require(len(set(keys)) == len(keys), 'duplicate candidate source')
    require(Counter(x['dataset'] for x in rows) == {'Social Bias Frames': 12, 'Civil Comments': 12, 'AMI Meeting Corpus': 6}, 'candidate source counts')
    groups = {}
    for row in rows:
        for k in ('record_id', 'group_id', 'source_url', 'license_url', 'attribution', 'selection_notes', 'retrieved_on'):
            require(type(row[k]) is str and bool(row[k].strip()), f'missing provenance {k}')
        require(row['assigned_split'] in COUNTS, 'candidate split')
        require(row['dataset'] in {'Social Bias Frames','Civil Comments','AMI Meeting Corpus'}, 'unknown candidate dataset')
        require(bool(row['original_text']), 'empty source snapshot')
        expected_license = 'CC0-1.0' if row['dataset'] == 'Civil Comments' else 'CC-BY-4.0'
        expected_license_url = ('https://creativecommons.org/publicdomain/zero/1.0/' if expected_license == 'CC0-1.0'
                                else 'https://creativecommons.org/licenses/by/4.0/')
        require(row['license'] == expected_license and row['license_url'] == expected_license_url, 'incorrect source licence metadata')
        if row['dataset'] == 'Social Bias Frames':
            require(row['version'] == 'SBIC.v2' and row['source_split'] in {'validation','test'}
                    and re.fullmatch(r'[A-Z0-9]{30}', row['record_id']), 'missing/invalid SBIC metadata')
            require(type(row['source_labels'].get('annotations')) is list and bool(row['source_labels']['annotations']), 'missing SBIC annotations')
        elif row['dataset'] == 'Civil Comments':
            require(re.fullmatch(r'[a-f0-9]{40}', row['version'] or '') and re.fullmatch(r'(validation|test):row:\d+', row['record_id']), 'missing Civil mirror revision/locator')
            require(row['record_id'].split(':')[0] == row['source_split'], 'Civil locator split mismatch')
            require(set(row['source_labels']) == {'toxicity','severe_toxicity','obscene','threat','insult','identity_attack','sexual_explicit'}, 'missing Civil labels')
            require(all(type(v) in {int,float} and 0 <= v <= 1 for v in row['source_labels'].values()), 'Civil score range')
        else:
            require(row['version'] == 'manual-1.6.2' and row['source_split'] in {'scenario-only:training','scenario-only:development'}
                    and row['record_id'].startswith(row['group_id']), 'missing AMI meeting/version metadata')
        key = (row['dataset'], row['group_id'])
        require(key not in groups or groups[key] == row['assigned_split'], 'related sources cross splits')
        groups[key] = row['assigned_split']
        snap = json.dumps(row['original_text'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        require(hashlib.sha256(snap.encode()).hexdigest() == row['snapshot_sha256'], 'source snapshot hash mismatch')
        require(type(row['source_labels']) is dict, 'source labels object')
    used = set()
    for r in records:
        for s in r['sources']:
            matches = [x for x in rows if (x['dataset'], x['record_id']) == (s['dataset'], s['record_id'])]
            require(len(matches) == 1, 'source missing from manifest')
            row = matches[0]; used.add(row['candidate_id'])
            require(row['selected_transcript_id'] == r['transcript_id'] and row['assigned_split'] == r['split'], 'manifest selection/split mismatch')
            for key, mk in [('version','version'), ('source_split','source_split'), ('url','source_url'), ('license','license'), ('source_labels','source_labels'), ('utterance_ids','utterance_ids'), ('changes','changes')]:
                require(s[key] == row[mk], f'manifest differs: {key}')
            if s['dataset'] == 'AMI Meeting Corpus':
                turns = row['original_text']
                require(type(turns) is list and len(turns) == len(s['utterance_ids']), 'AMI source turn count')
                require([t['start'] for t in turns] == sorted(t['start'] for t in turns), 'AMI onset order')
                for uid, turn in zip(s['utterance_ids'], turns):
                    u = next(x for x in r['utterances'] if x['utterance_id'] == uid)
                    require(s['changes']['speaker_map'].get(turn['speaker']) == u['speaker_id'], 'AMI speaker mismatch')
                    if uid in s['changes']['copied_utterances']:
                        require(u['text'] == turn['text'], 'AMI excerpt differs from snapshot')
            else:
                require(type(row['original_text']) is str, 'post snapshot text')
                for uid in s['changes']['copied_utterances']:
                    u = next(x for x in r['utterances'] if x['utterance_id'] == uid)
                    require(u['text'] in row['original_text'], 'copied excerpt differs from snapshot')
    if complete:
        require(used == {x['candidate_id'] for x in rows if x['selected_transcript_id']}, 'orphan manifest selection')


def verify_sources(rows, source_cache=None):
    """Re-fetch the small official transcript archives and two 100-row mirror pages.

    This is optional network auditing, never model evaluation or prompt tuning.
    Archive bytes stay in memory; no source corpus or credentials are written.
    """
    import io
    import tarfile
    import urllib.request
    import zipfile
    import xml.etree.ElementTree as ET
    import time
    def fetch(url):
        print(f'Checking source: {url}', flush=True)
        cached = None
        if source_cache is not None:
            if url.endswith('SBIC.v2.tgz'):
                cached = 'sbf'
            elif url.endswith('ami_public_manual_1.6.2.zip'):
                cached = 'ami'
            elif 'datasets-server.huggingface.co/rows?' in url:
                cached = 'civil_test.json' if '&split=test&' in url else 'civil.json'
        if cached is not None:
            return (source_cache/cached).read_bytes()
        started = time.monotonic()
        with urllib.request.urlopen(url, timeout=60) as response:
            chunks = []
            while True:
                require(time.monotonic()-started <= 120, 'Source download exceeded two minutes; retry or use prior source downloads')
                chunk = response.read(65536)
                if not chunk:
                    return b''.join(chunks)
                chunks.append(chunk)
    sbrows = [r for r in rows if r['dataset'] == 'Social Bias Frames']
    with tarfile.open(fileobj=io.BytesIO(fetch(sbrows[0]['source_url'])), mode='r:gz') as archive:
        for split, filename in [('validation','dev'), ('test','tst')]:
            original = list(csv.DictReader(io.TextIOWrapper(archive.extractfile(f'SBIC.v2.{filename}.csv'), encoding='utf-8')))
            for row in sbrows:
                if row['source_split'] != split:
                    continue
                matches = [x for x in original if x['HITId'] == row['record_id']]
                require(bool(matches) and all(x['post'] == row['original_text'] for x in matches), 'SBIC source ID/text not authentic')
                keys = ['offensiveYN','intentYN','sexYN','whoTarget','targetMinority','targetCategory','targetStereotype','speakerMinorityYN','dataSource']
                require(row['source_labels'] == {'annotations': [{k: x[k] for k in keys} for x in matches]}, 'SBIC source labels differ')
    civil = [r for r in rows if r['dataset'] == 'Civil Comments']
    version = civil[0]['version']
    def check_revision():
        info = json.loads(fetch('https://huggingface.co/api/datasets/google/civil_comments'))
        require(info['sha'] == version, 'Civil mirror changed: use recorded revision parquet; do not silently reassign row IDs')
    check_revision()
    for split in ('validation','test'):
        page = json.loads(fetch(f'https://datasets-server.huggingface.co/rows?dataset=google/civil_comments&config=default&split={split}&offset=0&length=100'))
        lookup = {str(x['row_idx']): x['row'] for x in page['rows']}
        for row in civil:
            if row['source_split'] != split:
                continue
            original = lookup[row['record_id'].split(':')[-1]]
            require(row['original_text'] in original['text'], 'Civil source excerpt not authentic')
            require(row['source_labels'] == {k:v for k,v in original.items() if k != 'text'}, 'Civil source labels differ')
    check_revision()
    ami = [r for r in rows if r['dataset'] == 'AMI Meeting Corpus']
    partition_doc = fetch('https://groups.inf.ed.ac.uk/ami/corpus/datasets.shtml').decode('utf-8')
    partition_text = re.sub('<[^>]+>', ' ', partition_doc)
    scenario = partition_text.split('Scenario-only Partition of meetings', 1)[1].split('Scenario-only K-fold', 1)[0]
    require('ES2002' in scenario.split('SB')[0] and 'ES2003' in scenario.split('SB')[1].split('SC')[0], 'AMI scenario partition changed')
    for row in ami:
        require(row['source_split'] == ('scenario-only:training' if row['group_id']=='ES2002' else 'scenario-only:development'), 'AMI source partition mismatch')
    ns = '{http://nite.sourceforge.net/}'
    with zipfile.ZipFile(io.BytesIO(fetch(ami[0]['source_url']))) as archive:
        meetings = {}
        for row in ami:
            meeting = row['record_id'].split(':')[0]
            if meeting not in meetings:
                turns = []
                for speaker in 'ABCD':
                    words = list(ET.fromstring(archive.read(f'words/{meeting}.{speaker}.words.xml')))
                    lookup = {w.get(ns+'id'): i for i,w in enumerate(words)}
                    for seg in ET.fromstring(archive.read(f'segments/{meeting}.{speaker}.segments.xml')):
                        ids = re.findall(r'id\(([^)]+)\)', seg[0].get('href'))
                        a,b = lookup[ids[0]], lookup[ids[-1]]
                        text = ' '.join(w.text for w in words[a:b+1] if w.tag == 'w' and w.text)
                        text = re.sub(r'\s+([,.?!;:])', r'\1', text)
                        if re.search(r'\w', text):
                            turns.append(dict(segment_id=seg.get(ns+'id'), speaker=speaker,
                                              start=float(seg.get('transcriber_start')), end=float(seg.get('transcriber_end')),
                                              text=text, word_ids=ids))
                meetings[meeting] = sorted(turns, key=lambda t:(t['start'], t['speaker']))
            turns = meetings[meeting]
            first, last = row['record_id'].split(':', 1)[1].split('..')
            ids = [t['segment_id'] for t in turns]
            require(row['original_text'] == turns[ids.index(first):ids.index(last)+1], 'AMI source text/order/speaker/timing differs')
    print('PASS: all 30 candidate IDs/locators, source texts, original labels and AMI contiguous speaker mappings verified against downloads')


def validate_tone(records, tones):
    refs = {(r['transcript_id'], u['utterance_id']): (r, u) for r in records for u in r['utterances']}
    seen = set()
    for t in tones:
        fields(t, dict(transcript_id=str, utterance_id=str, textual_stance=str, explanation=str,
                       scripted_delivery=(dict, type(None)), observed_acoustic_delivery=str, audio_evidence=(dict, type(None))), 'tone')
        key = (t['transcript_id'], t['utterance_id'])
        require(key in refs and key not in seen, 'tone duplicate/unknown reference'); seen.add(key)
        require(t['textual_stance'] in {'supportive','neutral','confrontational','unclear'}, 'stance enum')
        require(t['observed_acoustic_delivery'] in {'unknown','not_observed'}, 'text-only dataset cannot claim observed acoustics')
        require(t['audio_evidence'] is None, 'no audio reviewed for this release')
        if t['scripted_delivery'] is not None:
            d = t['scripted_delivery']
            fields(d, dict(basis=str, cue=str), 'scripted delivery')
            require(d['basis'] == 'scripted_or_simulated' and d['cue'] in {'calm','raised_volume','shouting','hesitant','tense','neutral_delivery'}, 'scripted basis/cue')
            r, u = refs[key]
            require(r['origin'] != 'source_excerpt', 'invented source delivery')
            require(all(u['utterance_id'] not in s['changes']['copied_utterances'] for s in r['sources']), 'scripted real-source excerpt')
    require(seen == set(refs), 'missing tone annotations')


def validate_all(root=ROOT, pilot=False):
    records = read_jsonl(root/'development.jsonl') + ([] if pilot else read_jsonl(root/'evaluation.jsonl'))
    for r in records:
        validate_record(r)
    require(len({r['transcript_id'] for r in records}) == len(records), 'duplicate transcript IDs')
    counts = {s: dict(Counter(r['annotations'][-1]['scenario_category'] for r in records if r['split'] == s)) for s in COUNTS}
    if pilot:
        require(len(records) == 3 and counts['development'] == dict.fromkeys(LABELS, 1), 'pilot counts')
    else:
        require(counts == COUNTS and {r['transcript_id'] for r in records} == {f'C{i:03}' for i in range(1,16)}, 'exact split/category/ID counts')
        for name, split in [('development.jsonl','development'), ('evaluation.jsonl','evaluation')]:
            require(all(r['split'] == split for r in read_jsonl(root/name)), 'wrong split file')
        validate_tone(records, read_jsonl(root/'tone_annotations.jsonl'))
        require(hashlib.sha256((root/'evaluation.jsonl').read_bytes()).hexdigest() == (root/'evaluation.sha256').read_text().strip(), 'evaluation lock changed')
    validate_manifest(records, read_manifest(root), complete=not pilot)
    require(not duplicate_pairs(records), f'potential duplicates: {duplicate_pairs(records)}')
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pilot', action='store_true', help='validate only the three initial development examples')
    parser.add_argument('--verify-sources', action='store_true', help='re-fetch public sources to check authenticity (network required)')
    parser.add_argument('--source-cache', type=Path, help='reuse prior sbf/ami archives and civil.json/civil_test.json responses outside Git; still checks live mirror revision and AMI partition')
    args = parser.parse_args()
    if args.source_cache and not args.verify_sources:
        parser.error('--source-cache requires --verify-sources')
    try:
        records = validate_all(pilot=args.pilot)
        if args.verify_sources:
            verify_sources(read_manifest(), args.source_cache)
    except (ValueError, KeyError, TypeError, UnicodeError, OSError) as e:
        parser.exit(1, f'Validation failed: {e}\n')
    print(f'PASS: {len(records)} conversations; schema, checkpoints, evidence, provenance, splits and duplicates')
    if not args.pilot:
        print('PASS: tone references, text-only acoustic constraints and evaluation SHA-256 lock')
        print('Final categories:', dict(Counter(r['annotations'][-1]['scenario_category'] for r in records)))
        print('Final decisions:', dict(Counter(r['annotations'][-1]['expected_decision'] for r in records)))
        print('Final utterance labels:', dict(Counter(l['label'] for r in records for l in r['annotations'][-1]['utterance_labels'])))
