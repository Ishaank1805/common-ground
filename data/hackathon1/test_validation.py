"""Malformed fixtures are copies of development records, never evaluation prompts."""
import copy
import tempfile
import unittest
from pathlib import Path
import validate as v
from export_inputs import input_only


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dev = v.read_jsonl(v.ROOT/'development.jsonl')
        cls.rows = v.read_manifest()

    def broken(self, edit):
        record = copy.deepcopy(self.dev[0])
        edit(record)
        with self.assertRaises((ValueError, TypeError)):
            v.validate_record(record)

    def test_valid_development(self):
        for record in self.dev:
            v.validate_record(record)

    def test_required_fields_and_types(self):
        self.broken(lambda r: r.pop('language'))
        self.broken(lambda r: r.update(language='hi'))
        self.broken(lambda r: r['utterances'][0].update(sequence=True))
        self.broken(lambda r: r.update(unapproved='metadata'))

    def test_ids_and_speakers(self):
        self.broken(lambda r: r['utterances'][1].update(utterance_id='U1'))
        self.broken(lambda r: r['utterances'][1].update(sequence=3))
        self.broken(lambda r: r['utterances'][0].update(speaker_id='Alice'))
        self.broken(lambda r: r['utterances'][0].update(speaker_id='P4'))

    def test_checkpoint_visibility_and_final(self):
        self.broken(lambda r: r['annotations'][0]['utterance_labels'].append(r['annotations'][-1]['utterance_labels'][-1]))
        self.broken(lambda r: r['annotations'][-1]['utterance_labels'].pop())
        self.broken(lambda r: r['annotations'].pop())
        self.broken(lambda r: r['annotations'].reverse())

    def test_enums_evidence_and_categories(self):
        self.broken(lambda r: r['annotations'][0]['utterance_labels'][1].update(label='toxic'))
        self.broken(lambda r: r['annotations'][0]['utterance_labels'][1].update(concern_types=['loud']))
        self.broken(lambda r: r['annotations'][0]['utterance_labels'][1].update(evidence_excerpts=['For a woman']))
        self.broken(lambda r: r['annotations'][0]['utterance_labels'][1].update(evidence_excerpts=[]))
        self.broken(lambda r: r['annotations'][-1].update(scenario_category='benign'))

    def test_decision_independent_of_category_and_nudge(self):
        self.assertEqual(self.dev[0]['annotations'][-1]['scenario_category'], 'concerning')
        self.assertEqual(self.dev[0]['annotations'][-1]['expected_decision'], 'no_nudge')
        self.broken(lambda r: r['annotations'][0].update(expected_decision='intervene'))
        self.broken(lambda r: r['annotations'][0].update(example_nudge=None))
        self.broken(lambda r: r['annotations'][-1].update(example_nudge='Please intervene.'))

    def test_source_adaptation_metadata(self):
        self.broken(lambda r: r.update(origin='synthetic'))
        self.broken(lambda r: r.update(speaker_mapping='observed'))
        self.broken(lambda r: r['sources'][0].update(record_id=''))
        self.broken(lambda r: r['sources'][0]['changes'].update(adapted_utterances=[]))
        self.broken(lambda r: r['sources'][0]['changes'].update(added_utterances=['U2']))

    def test_manifest_authenticity_and_split_links(self):
        for mutate in [lambda rows: rows[0].update(snapshot_sha256='fake'),
                       lambda rows: rows[0].update(record_id='invented'),
                       lambda rows: rows[0].update(source_labels={}),
                       lambda rows: rows[0].update(selected_transcript_id='C999')]:
            rows = copy.deepcopy(self.rows); mutate(rows)
            with self.assertRaises(ValueError):
                v.validate_manifest(self.dev, rows, complete=False)
        rows = copy.deepcopy(self.rows)
        ami = [x for x in rows if x['group_id'] == 'ES2002']
        ami[1]['assigned_split'] = 'evaluation'
        with self.assertRaises(ValueError):
            v.validate_manifest(self.dev, rows, complete=False)

    def test_ami_mapping(self):
        record = copy.deepcopy(next(r for r in self.dev if r['transcript_id']=='C005'))
        record['sources'][0]['changes']['speaker_map']['B'] = 'P1'
        with self.assertRaises(ValueError):
            v.validate_record(record)

    def test_duplicate_detection(self):
        record = copy.deepcopy(self.dev[0]); record['transcript_id'] = 'C002'
        self.assertTrue(v.duplicate_pairs([self.dev[0],record]))
        record['utterances'][0]['text'] += ' Actually.'
        self.assertTrue(v.duplicate_pairs([self.dev[0],record]))
        self.assertFalse(v.duplicate_pairs(self.dev))

    def test_tone_constraints(self):
        tones = [t for t in v.read_jsonl(v.ROOT/'tone_annotations.jsonl') if t['transcript_id'] in {r['transcript_id'] for r in self.dev}]
        v.validate_tone(self.dev, tones)
        for change in [dict(utterance_id='U99'), dict(observed_acoustic_delivery='shouting'),
                       dict(audio_evidence={'made_up':True}), dict(scripted_delivery={'basis':'observed','cue':'calm'}),
                       dict(textual_stance='angry')]:
            malformed = copy.deepcopy(tones); malformed[0].update(change)
            with self.assertRaises(ValueError):
                v.validate_tone(self.dev,malformed)
        with self.assertRaises(ValueError):
            v.validate_tone(self.dev, tones[:-1])

    def test_jsonl_utf8_and_complete_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'fixture.jsonl'
            for content in [b'{}\n\n', b'{\n}\n', b'\xff\n', b'{}', b'\xef\xbb\xbf{}\n', b'{"x":1,"x":2}\n', b'{"x":NaN}\n']:
                path.write_bytes(content)
                with self.assertRaises((ValueError, UnicodeError)):
                    v.read_jsonl(path)

    def test_input_export_allows_only_identifiers_and_text(self):
        result = input_only(self.dev[0], 'U2')
        self.assertEqual(set(result), {'transcript_id','utterances'})
        self.assertEqual(len(result['utterances']), 2)
        self.assertEqual(set(result['utterances'][0]), {'utterance_id','sequence','speaker_id','text'})
        self.assertNotIn('sources', result)
        self.assertNotIn('annotations', result)
        result['utterances'][0]['text']='changed copy'
        self.assertNotEqual(result['utterances'][0]['text'], self.dev[0]['utterances'][0]['text'])
        with self.assertRaises(ValueError):
            input_only(self.dev[0], 'U99')

    def test_counts_split_and_lock_mutations(self):
        # Use temporary files for structural checks only. Never send gold to a model.
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name in ['development.jsonl','evaluation.jsonl','tone_annotations.jsonl','provenance.csv','evaluation.sha256']:
                (root/name).write_bytes((v.ROOT/name).read_bytes())
            v.validate_all(root)
            original=(root/'evaluation.jsonl').read_bytes()
            (root/'evaluation.jsonl').write_bytes(original+b'\n')
            with self.assertRaises(ValueError): v.validate_all(root)
            (root/'evaluation.jsonl').write_bytes(original.replace(b'Author self-audit',b'Changed self-audit'))
            with self.assertRaises(ValueError): v.validate_all(root)
            (root/'evaluation.jsonl').write_bytes(original)
            (root/'development.jsonl').write_bytes((root/'development.jsonl').read_bytes().split(b'\n',1)[1])
            with self.assertRaises(ValueError): v.validate_all(root)


if __name__ == '__main__':
    unittest.main()
