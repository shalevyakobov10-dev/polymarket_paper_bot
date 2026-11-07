import unittest

from event_outcome import aggregate_event_outcome_evidence, classify_event_outcome


class EventOutcomeTests(unittest.TestCase):
    def test_rate_cut_question_maps_cut_headline_to_yes(self):
        result = classify_event_outcome("Will the Fed cut interest rates in December?", "Fed cuts interest rates")
        self.assertEqual(result["direction"], "YES")
        self.assertEqual(result["family"], "rate_down")

    def test_rate_cut_question_maps_hold_or_hike_to_no(self):
        for headline in ("Fed holds interest rates steady", "Fed hikes interest rates"):
            with self.subTest(headline=headline):
                result = classify_event_outcome("Will the Fed cut interest rates?", headline)
                self.assertEqual(result["direction"], "NO")

    def test_rate_raise_question_maps_hike_to_yes(self):
        result = classify_event_outcome("Will the Fed raise interest rates?", "Fed hikes interest rates")
        self.assertEqual(result["direction"], "YES")

    def test_negated_target_event_supports_no(self):
        result = classify_event_outcome("Will the Fed cut interest rates?", "Fed will not cut interest rates")
        self.assertEqual(result["direction"], "NO")

    def test_bill_passage_supports_and_opposes(self):
        self.assertEqual(classify_event_outcome("Will the bill pass?", "Senate approves bill")["direction"], "YES")
        self.assertEqual(classify_event_outcome("Will the bill pass?", "Senate rejects bill")["direction"], "NO")

    def test_election_and_product_launch_examples(self):
        self.assertEqual(classify_event_outcome("Will the candidate win the election?", "Candidate loses election")["direction"], "NO")
        self.assertEqual(classify_event_outcome("Will the company launch the product?", "Company delays product launch")["direction"], "NO")

    def test_speculative_or_unsupported_headlines_abstain(self):
        self.assertIsNone(classify_event_outcome("Will the Fed cut interest rates?", "Fed may cut interest rates")["direction"])
        self.assertIsNone(classify_event_outcome("Will the Fed cut interest rates?", "Fed discusses economic outlook")["direction"])
        self.assertIsNone(classify_event_outcome("Will Bitcoin trade above 100000 in December?", "Bitcoin rallies sharply")["direction"])

    def test_conflicting_cues_abstain(self):
        result = classify_event_outcome("Will the bill pass?", "Senate approves bill after opposition rejects the bill")
        self.assertIsNone(result["direction"])
        self.assertEqual(result["reason"], "conflicting_headline_cues")

    def test_aggregator_counts_one_vote_per_publisher(self):
        now = 1_800_000_000.0
        rows = [
            {"event_evidence_status": "decisive", "event_direction": "YES", "publisher_id": "a", "published_at_epoch": now - 60},
            {"event_evidence_status": "decisive", "event_direction": "YES", "publisher_id": "a", "published_at_epoch": now - 120},
            {"event_evidence_status": "decisive", "event_direction": "YES", "publisher_id": "b", "published_at_epoch": now - 180},
        ]
        result = aggregate_event_outcome_evidence(rows, now_epoch=now)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["direction"], "YES")
        self.assertEqual(result["publisher_count"], 2)
        self.assertEqual(result["article_count"], 3)
        self.assertEqual(result["score"], 1.0)
        self.assertIn("not_probability", result["interpretation"])

    def test_weak_cross_publisher_consensus_abstains(self):
        now = 1_800_000_000.0
        rows = [
            {"event_evidence_status": "decisive", "event_direction": "YES", "publisher_id": "a", "published_at_epoch": now - 60},
            {"event_evidence_status": "decisive", "event_direction": "YES", "publisher_id": "b", "published_at_epoch": now - 60},
            {"event_evidence_status": "decisive", "event_direction": "NO", "publisher_id": "c", "published_at_epoch": now - 60},
        ]
        result = aggregate_event_outcome_evidence(rows, now_epoch=now)
        self.assertFalse(result["qualified"])
        self.assertIsNone(result["direction"])
        self.assertEqual(result["consensus_ratio"], round(2 / 3, 6))

    def test_conflicting_articles_from_same_publisher_do_not_vote(self):
        now = 1_800_000_000.0
        rows = [
            {"event_evidence_status": "decisive", "event_direction": "YES", "publisher_id": "a", "published_at_epoch": now - 60},
            {"event_evidence_status": "decisive", "event_direction": "NO", "publisher_id": "a", "published_at_epoch": now - 120},
            {"event_evidence_status": "decisive", "event_direction": "YES", "publisher_id": "b", "published_at_epoch": now - 180},
        ]
        result = aggregate_event_outcome_evidence(rows, now_epoch=now)
        self.assertFalse(result["qualified"])
        self.assertEqual(result["conflict_publisher_count"], 1)
        self.assertEqual(result["publisher_count"], 1)

    def test_stale_and_unknown_timestamps_are_not_voters(self):
        now = 1_800_000_000.0
        rows = [
            {"event_evidence_status": "decisive", "event_direction": "YES", "publisher_id": "old", "published_at_epoch": now - 48 * 3600},
            {"event_evidence_status": "decisive", "event_direction": "YES", "publisher_id": "missing", "published_at_epoch": None},
        ]
        result = aggregate_event_outcome_evidence(rows, now_epoch=now)
        self.assertFalse(result["qualified"])
        self.assertEqual(result["stale_article_count"], 1)
        self.assertEqual(result["invalid_timestamp_count"], 1)

    def test_aggregator_rejects_invalid_consensus_threshold(self):
        with self.assertRaises(ValueError):
            aggregate_event_outcome_evidence([], now_epoch=1_800_000_000, minimum_consensus_ratio=0.5)


if __name__ == "__main__":
    unittest.main()
