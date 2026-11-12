import unittest
from decimal import Decimal
from service import map_record

class RegistryHarvestTests(unittest.TestCase):
    def test_maps_alias(self):
        self.assertEqual(map_record({'registered': '2020'}, {'founded': ['registered']}), {'founded': '2020'})

    def test_rejects_conflicting_aliases(self):
        with self.assertRaises(ValueError):
            map_record({'a': '1', 'b': '2'}, {'date': ['a', 'b']})


if __name__ == '__main__':
    unittest.main()
