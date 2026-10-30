"""The cheat baker's register-write scan (patch_nso.py cheat_written): a word stream is decoded the way the executor
decodes it, so a stored value is never misread as an opcode, and anything the scan cannot model marks every register
(refuses baking) rather than guessing. The cases come from the tables that motivated the scan: BotW 1.9.0 (its master
stores whole ARM instructions as values, which read as load / arithmetic opcodes writing r0) and TotK 1.4.3 (every code
write addressed through offset register A)."""
import ast
from pathlib import Path
import unittest

TOOLS = Path(__file__).resolve().parents[1]

# patch_nso.py is a command-line program whose top level loads and rewrites an image.
# Compile the scan without executing that driver, as test_jump_copies does for the recognizers.
tree = ast.parse((TOOLS / 'patch_nso.py').read_text())
scope = {}
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'cheat_written'], type_ignores=[]), str(TOOLS / 'patch_nso.py'), 'exec'), scope)
cheat_written = scope['cheat_written']


def store(rel, val, ofs=0, width=4):
    """0TMR00AA AAAAAAAA YYYYYYYY (YYYYYYYY): a store at main+rel + the offset register, as the tables spell it."""
    return [width << 24 | ofs << 16 | (rel >> 32) & 0xFF, rel & 0xFFFFFFFF] + ([val & 0xFFFFFFFF, val >> 32] if width == 8 else [val & 0xFFFFFFFF])


class WrittenScan(unittest.TestCase):
    def test_stored_values_are_not_opcodes(self):
        # BotW's master: the values are ARM instruction words; 0x51000529 / 0x71000508 / 0x94211D5A read as opcodes
        # 5 / 7 / 9 whose destination nibble is 0 -- register 0 is the no-offset register every plain store names
        words = store(0xCC7174, 0xD503201F) + store(0xCDA74C, 0x51000529) + store(0xC973E8, 0x71000508) + \
            store(0xCE9B60, 0x14000002) + store(0xCD5A80, 0x94211D5A)
        self.assertEqual(cheat_written(words), set())

    def test_offset_register_stays_bakeable(self):
        # TotK's shape: every code write through register A, a real load immediate into r15 ahead of them
        words = [0x400F0000, 0xDEADBEEF, 0x00000000] + store(0x1234, 0xD503201F, ofs=0xA)
        self.assertEqual(cheat_written(words), {0xF})

    def test_writer_opcodes(self):
        for words, want in [
            ([0x30020000, 0x00000005], {2}),                       # a loop start: the counter, then the count word
            ([0x31020000], {2}),                                    # a loop end decrements the same counter
            ([0x58030000, 0x00102030], {3}),                        # load from memory: address word consumed
            ([0x64123000, 0, 0], {2}),                              # store register with post-increment; value is a u64
            ([0x70040000, 0x1000], {4}),                            # arithmetic, immediate
            ([0x98010100, 0, 0], {1}),                              # arithmetic register with a u64 immediate
            ([0x40050000, 0, 0], {5}),                              # load immediate: a u64
        ]:
            self.assertEqual(cheat_written(words), want, f'{[hex(w) for w in words]}')

    def test_extended_opcodes(self):
        self.assertEqual(cheat_written([0xC1030000]), {3})          # C1 restore into r3
        self.assertEqual(cheat_written([0xC1030030]), {3})          # C1 clear r3 (op_type 3)
        self.assertEqual(cheat_written([0xC1030010]), set())        # C1 save (writes v_saved, not v_r)
        self.assertEqual(cheat_written([0xC2000001]), set(range(0x10)))   # a restore mask touches any register
        self.assertEqual(cheat_written([0xC3000180]), {0})          # C3 static read into r0 (idx 0x18 < 0x80)
        self.assertEqual(cheat_written([0xC3000880]), set())        # C3 write to a static register leaves v_r alone
        self.assertEqual(cheat_written([0xC4000000, 0, 0]), set())  # extended keypress: its 64-bit mask consumed
        self.assertEqual(cheat_written([0xFFF00404]), set())        # log of a register (comp 4)
        self.assertEqual(cheat_written([0xFFF00000, 0x1234]), set())   # log of memory: its address consumed

    def test_keypress_consumes_nothing(self):
        # the word after a keypress mask is an opcode again: this one is a real load into r0
        self.assertEqual(cheat_written([0x80000000, 0x51000529, 0x00102030]), {0})

    def test_register_memory_condition_preserves_following_writer(self):
        for comp in (0, 2):
            with self.subTest(comp=comp):
                # Nested conditions, then rA = 0x10. Missing C0's address word makes the scanner treat
                # 0x1000 and 0x10 as stores and swallow the load and both end markers, reporting no writes.
                master = [0x80000000, 0xC0450000 | comp << 8, 0x1000,
                          0x400A0000, 0, 0x10, 0x20000000, 0x20000000]
                written = cheat_written(master) | cheat_written(store(0x100, 0xD503201F, ofs=0xA))
                self.assertEqual(written, {0xA})  # refuse baking at +0x100: the store can target +0x110

    def test_register_memory_condition_address_is_not_an_opcode(self):
        for comp in (0, 2):
            for width in (4, 8):
                with self.subTest(comp=comp, width=width):
                    words = [0xC0050000 | width << 20 | comp << 8, 0x51000529, 0x20000000]
                    self.assertEqual(cheat_written(words), set())

    def test_register_condition_other_operand_shapes(self):
        for comp, width, operands in [(comp, 4, []) for comp in (1, 3, 5)] + [
            (4, width, [0x51000529] if width != 8 else [0x51000529, 0x71000508])
            for width in (1, 2, 4, 8)
        ]:
            with self.subTest(comp=comp, width=width):
                words = [0xC0050000 | width << 20 | comp << 8] + operands + [
                    0x400A0000, 0, 0x10, 0x20000000]
                self.assertEqual(cheat_written(words), {0xA})

    def test_register_memory_condition_truncated_address_refuses_baking(self):
        for comp in (0, 2):
            with self.subTest(comp=comp):
                self.assertEqual(cheat_written([0xC0450000 | comp << 8]), set(range(0x10)))

    def test_unknown_or_truncated_marks_every_register(self):
        for words in ([0xB0000000], [0xD1000000], [0xC5000000], [0xF2000000], [0xFFF20000],   # undefined opcodes
                      [0x04000000], [0x04000000, 0x1234]):                                    # operands cut off
            self.assertEqual(cheat_written(words), set(range(0x10)), f'{[hex(w) for w in words]}')


if __name__ == '__main__':
    unittest.main()
