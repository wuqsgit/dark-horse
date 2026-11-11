import asyncio
import time
import unittest
from unittest.mock import patch

from api import main
from minute_pipeline.writer import CandleBatchWriter


class QueryIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_heavy_read_does_not_block_event_loop(self):
        pairs = [
            (main.get_latest_alpha_scan, '_get_latest_alpha_scan_sync', ()),
            (main.get_alpha_trade_candidates, '_get_alpha_trade_candidates_sync', ()),
            (main.get_backtest_summary, '_get_backtest_summary_sync', ()),
            (main.get_alpha_symbol_detail, '_get_alpha_symbol_detail_sync', ('AX',)),
            (main.get_symbol_detail, '_get_symbol_detail_sync', ('BTCUSDT',)),
        ]
        for handler, helper, args in pairs:
            with self.subTest(handler=handler.__name__):
                def slow(*args):
                    time.sleep(0.15)
                    return {'ok': True}
                with patch.object(main, helper, side_effect=slow):
                    task = asyncio.create_task(handler(*args, user='admin'))
                    await asyncio.sleep(0.025)
                    self.assertFalse(task.done())
                    self.assertEqual(await task, {'ok': True})

    async def test_writer_coalesces_staggered_arrivals_and_drains_on_stop(self):
        writer = CandleBatchWriter(flush_seconds=0.1)
        stop = asyncio.Event()
        batches = []
        async def write(batch):
            batches.append(batch)
        writer._write_batch_resilient = write
        task = asyncio.create_task(writer.run(stop))
        await writer.put('alpha', {'symbol': 'A'})
        await asyncio.sleep(0.02)
        await writer.put('alpha', {'symbol': 'B'})
        await asyncio.wait_for(writer.queue.join(), 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual([len(b) for b in batches], [2])
