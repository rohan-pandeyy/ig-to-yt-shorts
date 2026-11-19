"""URL parsing, title/description limits and queue store rules. Run: .venv/bin/python -m unittest -v"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ig_to_yt_shorts as m  # noqa: E402
import store as s  # noqa: E402


class StoreAndPipelineTest(unittest.TestCase):
    def test_rules(self):

        # --- URL parsing
        ok = ['https://www.instagram.com/reel/DAbc_12-x/', 'https://instagram.com/reels/DAbc_12-x?igsh=xyz',
              'https://www.instagram.com/p/DAbc_12-x/?utm_source=ig', 'https://www.instagram.com/someuser/reel/DAbc_12-x/']
        for u in ok:
            assert m.parse_reel_url(u)[0] == 'DAbc_12-x', u
        assert m.parse_reel_url(ok[1])[1] == 'https://www.instagram.com/reel/DAbc_12-x/'
        for bad in ['file:///etc/passwd', 'http://192.168.1.1/reel/x', 'https://evil.com/instagram.com/reel/x',
                    'https://www.instagram.com.evil.com/reel/x/', 'https://www.instagram.com/someuser/', '']:
            assert m.parse_reel_url(bad) is None, bad

        # --- description / title
        d = m.build_description('Nice <b>day</b> 🌞 #travel')
        assert d == 'Nice bday/b 🌞 #travel\n\n#Shorts', repr(d)
        assert m.build_description('already #Shorts') == 'already #Shorts'
        big = m.build_description('🌞' * 3000)
        assert len(big.encode()) <= 5000 and big.endswith('#Shorts')
        assert m.build_description('') == '#Shorts'
        assert len(m.build_title('x' * 300)) == 100

        # --- store
        s.DB_PATH = os.path.join(tempfile.mkdtemp(), 'q.db')
        s.init_db()
        assert s.get_setting('interval_minutes') == '60'
        a = s.add_reel('https://www.instagram.com/reel/AAA/')
        b = s.add_reel('https://www.instagram.com/reel/BBB/?igsh=1')
        c = s.add_reel('https://www.instagram.com/reel/CCC/')
        for dup in ['https://instagram.com/reels/AAA?igsh=2', 'https://www.instagram.com/p/AAA/']:
            try: s.add_reel(dup); raise SystemExit('dup accepted')
            except s.QueueError as e: assert 'Already in the queue' in str(e)
        try: s.add_reel('https://example.com/x'); raise SystemExit('bad accepted')
        except s.QueueError: pass

        # Upload Now on C goes first in priority lane; FIFO lane still starts at A
        s.request_upload_now(c['id'], '  My title ')
        assert s.claim_next(priority_only=True)['id'] == c['id']
        assert s.get_reel(c['id'])['title_override'] == 'My title'
        assert s.claim_next(priority_only=True) is None
        r = s.claim_next(priority_only=False); assert r['id'] == a['id'] and r['status'] == 'downloading'
        # A can't be claimed twice, can't be deleted mid-run
        assert s.claim_next(priority_only=False)['id'] == b['id']
        assert s.claim_next(priority_only=False) is None
        try: s.delete_reel(a['id']); raise SystemExit('deleted in-progress')
        except s.QueueError as e: assert 'downloading' in str(e)

        # crash recovery: A downloading -> pending, B uploading -> needs_review
        s.update_reel(b['id'], status='uploading', tags=['x', 'y'])
        assert s.get_reel(b['id'])['tags'] == ['x', 'y']
        s.update_reel(c['id'], status='failed', error='boom')
        assert s.recover_interrupted() == (1, 1)
        assert s.get_reel(a['id'])['status'] == 'pending'
        assert s.get_reel(b['id'])['status'] == 'needs_review'

        # failed C: retry returns it to FIFO (ahead of later reels), priority cleared
        d_ = s.add_reel('https://www.instagram.com/reel/DDD/')
        s.retry_reel(c['id'])
        rc = s.get_reel(c['id']); assert rc['status'] == 'pending' and rc['priority_seq'] is None and rc['title_override'] == 'My title'
        assert [r['id'] for r in [s.claim_next(False), s.claim_next(False)]] == [a['id'], c['id']]
        # uploaded can't be removed or re-queued; needs_review -> mark done
        s.mark_done(b['id'])
        try: s.delete_reel(b['id']); raise SystemExit('deleted uploaded')
        except s.QueueError: pass
        try: s.request_upload_now(b['id']); raise SystemExit('re-upload allowed')
        except s.QueueError: pass
        s.delete_reel(d_['id']); assert s.get_reel(d_['id']) is None
        s.set_setting('interval_minutes', 30); assert s.get_setting('interval_minutes') == '30'


if __name__ == '__main__':
    unittest.main()
