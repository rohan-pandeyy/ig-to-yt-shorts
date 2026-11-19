"""YouTube title/description rules. Run: .venv/bin/python -m unittest -v"""

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ig_to_yt_shorts as yt  # noqa: E402
import store  # noqa: E402

URL = 'https://www.instagram.com/reel/DAbc123/'
ATTRIB = 'Originally posted by @chef.anna on Instagram\n' + URL + '\n\n#Shorts'


def flat(text):
    return ' '.join(text.split())


def rejoin(title, overflow):
    """Undo a split: the caption must be fully recoverable (modulo whitespace)."""
    if not overflow:
        return title
    head = title[:-len(yt.ELLIPSIS)]
    joined_word = head + overflow           # first word was cut mid-word
    spaced = head + ' ' + overflow          # normal split between words
    return spaced, joined_word


class SplitCaptionTest(unittest.TestCase):
    def assertLossless(self, caption):
        title, overflow = yt.split_caption(caption)
        self.assertLessEqual(yt.yt_len(title), 100, title)
        original = flat(yt._safe(caption))
        if not overflow:
            self.assertEqual(title, original)
        else:
            self.assertTrue(title.endswith('...'))
            self.assertIn(original, [flat(x) for x in rejoin(title, overflow)])
        return title, overflow

    def test_short_caption_is_the_whole_title(self):
        self.assertEqual(yt.split_caption('Sunset at the beach 🌅 #travel'),
                         ('Sunset at the beach 🌅 #travel', ''))

    def test_exactly_100_fits_101_splits(self):
        exact = ('word ' * 20)[:99] + 'x'                  # 100 chars, ends in a word
        self.assertEqual(len(exact), 100)
        self.assertEqual(yt.split_caption(exact), (exact, ''))
        over = exact + 'y'
        title, overflow = self.assertLossless(over)
        self.assertTrue(overflow)

    def test_long_caption_splits_between_words(self):
        caption = ('This is an example of a much longer Instagram caption that exceeds the '
                   'YouTube Shorts title limit and needs to be split intelligently between the '
                   'title and description.')
        title, overflow = self.assertLossless(caption)
        self.assertEqual(title, 'This is an example of a much longer Instagram caption that exceeds '
                                'the YouTube Shorts title limit...')
        self.assertEqual(overflow, 'and needs to be split intelligently between the title and description.')
        self.assertEqual(len(title), 100)                  # adding " and" would make it 104

    def test_never_cuts_a_word_and_stays_within_limit(self):
        rng = random.Random(7)
        vocab = ['a', 'be', 'cat', 'door', 'eagle', 'flower', 'giraffes', '#hashtag', '🌞', '👩‍👩‍👧',
                 'naïve', 'मुंबई', '@friend', 'https://example.com/x', 'end.']
        for _ in range(500):
            caption = ' '.join(rng.choice(vocab) for _ in range(rng.randint(1, 60)))
            title, overflow = self.assertLossless(caption)
            if overflow:
                kept_words = title[:-3].split(' ')
                self.assertTrue(set(kept_words) <= set(vocab), title)   # only whole words

    def test_line_breaks_flattened_in_title_preserved_in_overflow(self):
        caption = ('Day 3 in Lisbon\nWe found the best pastel de nata in town and honestly it changed '
                   'everything about breakfast\n\nRecipe below 👇\n1. Pastry\n2. Custard\n\n#lisbon #food')
        title, overflow = self.assertLossless(caption)
        self.assertNotIn('\n', title)
        self.assertTrue(title.startswith('Day 3 in Lisbon We found'))
        self.assertIn('\n\nRecipe below 👇\n1. Pastry\n2. Custard\n\n#lisbon #food', overflow)

    def test_emojis_count_conservatively_and_are_never_split(self):
        family = '👩‍👩‍👧'                                     # ZWJ sequence: 8 UTF-16 units
        self.assertEqual(yt.yt_len('🌞'), 2)
        caption = ' '.join([family] * 30)
        title, overflow = self.assertLossless(caption)
        self.assertLessEqual(yt.yt_len(title), 100)
        self.assertTrue(all(w == family for w in title[:-3].split(' ')))

    def test_hashtags_and_mentions_kept_verbatim(self):
        caption = 'New drop with @studio.kai 🔥 #streetwear #ootd #fashion #style #mensfashion #outfit #trend #design #art'
        title, overflow = self.assertLossless(caption)
        self.assertIn('@studio.kai', title)
        self.assertTrue(title.startswith('New drop with @studio.kai 🔥 #streetwear'))

    def test_angle_brackets_replaced_not_dropped(self):
        self.assertEqual(yt.split_caption('I <3 this -> wow'), ('I ‹3 this -› wow', ''))

    def test_single_huge_word_is_the_only_mid_word_cut(self):
        url = 'https://example.com/' + 'a' * 150
        title, overflow = self.assertLossless(url + ' tail')
        self.assertEqual(len(title), 100)
        self.assertEqual(title[:-3] + overflow, url + ' tail')

    def test_empty_caption(self):
        self.assertEqual(yt.split_caption(''), ('', ''))
        self.assertEqual(yt.split_caption(None), ('', ''))
        self.assertEqual(yt.split_caption('  \n '), ('', ''))


class MetadataTest(unittest.TestCase):
    def test_short_caption(self):
        title, desc = yt.build_youtube_metadata('Sunday reset ☕️', 'chef.anna', 'Anna', URL)
        self.assertEqual(title, 'Sunday reset ☕️')
        self.assertEqual(desc, 'Originally posted by @chef.anna (Anna) on Instagram\n' + URL + '\n\n#Shorts')

    def test_long_caption_overflow_goes_first_in_description(self):
        caption = ('This is an example of a much longer Instagram caption that exceeds the '
                   'YouTube Shorts title limit and needs to be split intelligently between the '
                   'title and description.')
        title, desc = yt.build_youtube_metadata(caption, 'chef.anna', None, URL)
        self.assertTrue(title.endswith('title limit...'))
        self.assertEqual(desc, 'and needs to be split intelligently between the title and '
                               'description.\n\n' + ATTRIB)
        self.assertNotIn('#Shorts', title)

    def test_empty_caption_fallback(self):
        self.assertEqual(yt.build_youtube_metadata('', 'chef.anna', None, URL),
                         ('Instagram Reel by @chef.anna', ATTRIB))
        title, desc = yt.build_youtube_metadata(None, None, None, None)
        self.assertEqual((title, desc), ('Instagram Reel', 'Originally posted on Instagram\n\n#Shorts'))

    def test_custom_title_wins_and_description_keeps_whole_caption(self):
        caption = 'Line one of the caption\nline two #food'
        title, desc = yt.build_youtube_metadata(caption, 'chef.anna', None, URL,
                                                custom_title='  My   custom title  ')
        self.assertEqual(title, 'My custom title')
        self.assertEqual(desc, caption + '\n\n' + ATTRIB)

    def test_blank_custom_title_means_automatic(self):
        self.assertEqual(yt.build_youtube_metadata('Hello', 'chef.anna', None, URL, custom_title='   ')[0],
                         'Hello')

    def test_custom_title_limits(self):
        self.assertEqual(yt.clean_custom_title('x' * 100), 'x' * 100)
        with self.assertRaises(ValueError):
            yt.clean_custom_title('x' * 101)
        with self.assertRaises(ValueError):
            yt.clean_custom_title('🌞' * 51)              # 102 UTF-16 units
        self.assertEqual(yt.clean_custom_title('a <b> c'), 'a ‹b› c')

    def test_full_name_same_as_handle_not_repeated(self):
        _, desc = yt.build_youtube_metadata('hi', 'chef.anna', 'chef.anna', URL)
        self.assertTrue(desc.startswith('Originally posted by @chef.anna on Instagram'))

    def test_description_within_5000_bytes_and_attribution_survives(self):
        caption = ' '.join(['मुंबई'] * 1100)               # ~2200 chars of Devanagari, ~16 KB
        title, desc = yt.build_youtube_metadata(caption, 'chef.anna', None, URL)
        self.assertLessEqual(len(desc.encode('utf-8')), 5000)
        self.assertTrue(desc.endswith(ATTRIB))
        self.assertIn('[caption shortened: YouTube description limit]', desc)
        self.assertNotIn('�', desc)

    def test_typical_long_caption_fits_entirely(self):
        caption = ' '.join(['Delicious'] * 240)          # ~2200 chars, Instagram's maximum
        title, desc = yt.build_youtube_metadata(caption, 'chef.anna', None, URL)
        self.assertNotIn('shortened', desc)
        self.assertEqual(flat(title[:-3] + ' ' + desc.split('\n\nOriginally')[0]), caption)

    def test_deterministic(self):
        args = ('A caption that is long enough to be split ' * 5, 'chef.anna', 'Anna', URL)
        self.assertEqual(yt.build_youtube_metadata(*args), yt.build_youtube_metadata(*args))


class StoreMigrationTest(unittest.TestCase):
    def test_old_database_gets_new_columns_and_keeps_rows(self):
        import sqlite3
        store.DB_PATH = os.path.join(tempfile.mkdtemp(), 'old.db')
        conn = sqlite3.connect(store.DB_PATH)
        old_schema = store.SCHEMA.replace(
            "    uploader_handle       TEXT,          -- @username, for attribution\n", '').replace(
            "    uploader_name         TEXT,          -- display name, for attribution\n", '')
        conn.executescript(old_schema)
        conn.execute("INSERT INTO reels (shortcode, url, status, title, created_at, updated_at) "
                     "VALUES ('X', 'u', 'uploaded', 'Video by x', 't', 't')")
        conn.commit()
        conn.close()
        store.init_db()
        store.init_db()                                   # idempotent
        row = store.list_reels()[0]
        self.assertEqual((row['status'], row['title'], row['uploader_handle']), ('uploaded', 'Video by x', None))

    def test_upload_now_rejects_overlong_title(self):
        store.DB_PATH = os.path.join(tempfile.mkdtemp(), 'q.db')
        store.init_db()
        rid = store.add_reel(URL)['id']
        with self.assertRaises(store.QueueError):
            store.request_upload_now(rid, 'x' * 101)
        self.assertEqual(store.request_upload_now(rid, 'x' * 100)['title_override'], 'x' * 100)


if __name__ == '__main__':
    unittest.main()
