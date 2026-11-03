import unittest

from publisher_identity import publisher_identity


class PublisherIdentityTests(unittest.TestCase):
    def test_known_rss_sections_share_one_identity(self):
        self.assertEqual(publisher_identity("BBC News - World"), "bbc")
        self.assertEqual(publisher_identity("BBC News - Business"), "bbc")
        self.assertEqual(publisher_identity("The Guardian - Science"), "guardian")
        self.assertEqual(publisher_identity("Al Jazeera English"), "al-jazeera")

    def test_gdelt_article_domains_map_to_known_publishers(self):
        self.assertEqual(
            publisher_identity("www.bbc.com", article_url="https://www.bbc.com/news/a"),
            "bbc",
        )
        self.assertEqual(publisher_identity("feeds.bbci.co.uk"), "bbc")
        self.assertEqual(publisher_identity("www.dw.com"), "dw")
        self.assertEqual(publisher_identity("www.france24.com"), "france24")

    def test_article_domain_is_used_when_source_label_is_generic(self):
        self.assertEqual(
            publisher_identity("Unknown feed", "https://rss.example.org/feed", "https://news.example.org/story"),
            "example.org",
        )

    def test_two_level_public_suffix_keeps_registered_domain(self):
        self.assertEqual(
            publisher_identity("publisher.example.co.uk", article_url="https://publisher.example.co.uk/story"),
            "example.co.uk",
        )

    def test_non_domain_source_label_uses_safe_normalized_fallback(self):
        self.assertEqual(publisher_identity("Example News"), "example-news")
        self.assertEqual(publisher_identity("Reuters"), "reuters")

    def test_empty_and_malformed_sources_return_empty_or_safe_identity(self):
        self.assertEqual(publisher_identity(), "")
        self.assertEqual(publisher_identity("https://"), "")


if __name__ == "__main__":
    unittest.main()
