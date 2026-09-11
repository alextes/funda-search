import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import listing_facts as facts


SOURCE = 'VvE € 190 per maand. Eigen grond.'
RESULT = {'vve': {'summary': '€190 per month', 'monthly_eur': 190,
                  'evidence': ['VvE € 190 per maand.']},
          'erfpacht': {'summary': 'Own ground', 'evidence': ['Eigen grond.']}}


class ExtractionTests(unittest.TestCase):
    def test_evidence_must_be_in_source(self):
        self.assertEqual(facts.validate(copy.deepcopy(RESULT), SOURCE), RESULT)
        invalid = copy.deepcopy(RESULT)
        invalid['erfpacht']['evidence'] = ['Perpetually paid off']
        with self.assertRaises(ValueError):
            facts.validate(invalid, SOURCE)
        invalid['erfpacht']['evidence'] = []
        with self.assertRaises(ValueError):
            facts.validate(invalid, SOURCE)

    def test_characteristics_keep_ownership_and_costs_not_price(self):
        raw = {'KenmerkSections': [
            {'Title': 'Overdracht', 'KenmerkenList': [
                {'Label': 'Vraagprijs', 'Value': '€ 500000'},
                {'Label': 'Bijdrage VvE', 'Value': '€ 190 per maand'}]},
            {'Title': 'Kadastrale gegevens', 'KenmerkenList': [
                {'Label': 'Eigendomssituatie', 'Value': 'Volle eigendom'}]}]}
        text = facts.characteristics(SimpleNamespace(raw=raw))
        self.assertIn('190', text)
        self.assertIn('Volle eigendom', text)
        self.assertNotIn('500000', text)

    def test_cached_facts_invalidate_on_source_change(self):
        listing = {'description': SOURCE}
        listing['quick_facts'] = {**RESULT, 'version': facts.VERSION,
                                  'source_hash': facts.source_hash(listing)}
        self.assertIsNotNone(facts.current_facts(listing))
        listing['description'] += ' Canon verschuldigd.'
        self.assertIsNone(facts.current_facts(listing))

    def test_api_shape_and_incomplete_response(self):
        api_result = copy.deepcopy(RESULT)
        api_result['vve']['evidence'] = [1]
        api_result['erfpacht']['evidence'] = [1]
        response = {'status': 'completed', 'output': [
            {'content': [{'type': 'output_text', 'text': json.dumps(api_result)}]}]}
        with patch.object(facts.urllib.request, 'urlopen') as urlopen:
            handle = MagicMock()
            handle.read.return_value = json.dumps(response)
            urlopen.return_value.__enter__.return_value = handle
            extracted = facts.extract({'description': SOURCE}, api_key='test-key')
            self.assertEqual(extracted['vve']['monthly_eur'], 190)
            request = urlopen.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(payload['model'], "gpt-5.6-luna")
            self.assertEqual(payload['reasoning'], {"effort": "low"})
            self.assertEqual(extracted['reasoning_effort'], "low")
            self.assertFalse(payload['store'])
            self.assertTrue(payload['text']['format']['strict'])
            response['status'] = 'incomplete'
            handle.read.return_value = json.dumps(response)
            with self.assertRaises(ValueError):
                facts.extract({'description': SOURCE}, api_key='test-key')

    @patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'})
    def test_priority_limit_cache_and_failure_cooldown(self):
        listings = {str(i): {'id': str(i), 'description': SOURCE} for i in range(4)}
        with patch.object(facts, 'extract', side_effect=lambda l: {
            **RESULT, 'version': facts.VERSION, 'source_hash': facts.source_hash(l),
            'model': facts.MODEL, 'reasoning_effort': facts.REASONING_EFFORT
        }) as extract:
            self.assertEqual(facts.enrich(listings, limit=2, ratings={'1': 3}, new_ids={'2'}), 2)
            self.assertEqual([c.args[0]['id'] for c in extract.call_args_list], ['2', '1'])
        with patch.object(facts, 'extract', side_effect=ValueError('bad evidence')) as extract:
            self.assertEqual(facts.enrich(listings, limit=2), 1)
            self.assertIn('facts_retry', listings['0'])
        with patch.object(facts, 'extract', return_value={**RESULT}) as extract:
            facts.enrich(listings, limit=2)
            self.assertEqual([c.args[0]['id'] for c in extract.call_args_list], ['3'])

    @patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'})
    def test_previous_model_remains_visible_but_is_reextracted(self):
        listing = {'id': '1', 'description': SOURCE}
        listing['quick_facts'] = {**RESULT, 'version': facts.VERSION,
            'source_hash': facts.source_hash(listing), 'model': 'gpt-5.4-nano-2026-03-17'}
        self.assertIsNotNone(facts.current_facts(listing))
        with patch.object(facts, 'extract', return_value={**listing['quick_facts'],
                'model': facts.MODEL, 'reasoning_effort': facts.REASONING_EFFORT}) as extract:
            self.assertEqual(facts.enrich({'1': listing}), 1)
            self.assertEqual(facts.enrich({'1': listing}), 0)
            extract.assert_called_once()

    @patch.dict('os.environ', {}, clear=True)
    def test_missing_key_is_noop(self):
        with patch.object(facts, 'extract') as extract:
            self.assertEqual(facts.enrich({'1': {'id': '1', 'description': SOURCE}}), 0)
            extract.assert_not_called()


if __name__ == '__main__':
    unittest.main()
