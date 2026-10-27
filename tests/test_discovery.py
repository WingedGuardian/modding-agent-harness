import unittest

from modding_harness.discovery import parse, request


def candidate(**extra):
    return dict(modId=4, name="A mod", version="1.2", author="Author",
                updatedAt="2026-10-15T00:00:00Z", status="published", **extra)


class DiscoveryTests(unittest.TestCase):
    def test_game_and_search_are_variables_never_query_interpolation(self):
        data = request("x4foundations", '雪"}){evil', 7)
        self.assertNotIn("evil", data["query"])
        self.assertEqual(data["variables"]["f"]["nameStemmed"][0]["value"], '雪"}){evil')
        self.assertEqual(data["variables"]["count"], 7)

    def test_invalid_scope_never_builds_a_request(self):
        for domain, name, count in (("../game", "a", 2), ("skyrim", "", 2),
                                    ("skyrim", "a", True), ("skyrim", "a", 101)):
            with self.assertRaises(ValueError):
                request(domain, name, count)

    def test_complete_and_zero_have_a_denominator(self):
        for nodes, expected in (([candidate()], "complete"), ([], "zero")):
            result = parse({"data": {"mods": {"totalCount": len(nodes), "nodes": nodes}}}, "skyrimspecialedition", "a")
            self.assertEqual(result["state"], expected)
            self.assertEqual(result["installed_evidence"], "not_checked")

    def test_errors_and_bad_shapes_cannot_be_zero(self):
        for payload in (None, {}, {"errors": [{"message": "secret"}]},
                        {"data": {"mods": {"nodes": []}}},
                        {"errors": ["secret"], "data": {"mods": {"nodes": [], "totalCount": 0}}}):
            result = parse(payload, "x4foundations", "a")
            self.assertEqual(result["state"], "unavailable")
            self.assertNotIn("secret", str(result))

    def test_cap_malformed_and_unknown_fields_are_partial(self):
        for page in ({"totalCount": 5, "nodes": [candidate()]},
                     {"totalCount": 2, "nodes": [candidate(), None]},
                     {"totalCount": 1, "nodes": [{"modId": 3, "name": "old"}]}):
            result = parse({"data": {"mods": page}}, "x4foundations", "a")
            self.assertEqual(result["state"], "partial")


if __name__ == "__main__":
    unittest.main()
