import unittest

from ingest.crawl.robots import applicable_crawl_delay


ROBOTS = """
User-agent: *
Disallow: /private
Crawl-delay: 2

User-agent: CatalogIngest
Crawl-delay: 5
Disallow: /secret
"""


class RobotsTests(unittest.TestCase):
    def test_specific_agent_delay_wins(self):
        delay = applicable_crawl_delay(ROBOTS, "Mozilla/5.0 CatalogIngest/1.0")
        self.assertEqual(delay, 5)

    def test_star_delay_is_used_for_other_agents(self):
        delay = applicable_crawl_delay(ROBOTS, "SomeOtherBot/1.0")
        self.assertEqual(delay, 2)

    def test_parser_honors_star_disallow(self):
        from urllib.robotparser import RobotFileParser

        parser = RobotFileParser()
        parser.parse(
            [
                "User-agent: *",
                "Disallow: /private",
                "Allow: /",
            ]
        )
        agent = "Mozilla/5.0 CatalogIngest/1.0"
        self.assertFalse(parser.can_fetch(agent, "https://shop.example/private/page"))
        self.assertTrue(parser.can_fetch(agent, "https://shop.example/en_us/women"))


if __name__ == "__main__":
    unittest.main()
