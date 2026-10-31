"""The cheat baker's register-write scan (patch_nso.py cheat_written): a word stream is decoded the way the executor
decodes it, so a stored value is never misread as an opcode, and anything the scan cannot model marks every register
(refuses baking) rather than guessing. The cases come from the tables that motivated the scan: BotW 1.9.0 (its master
stores whole ARM instructions as values, which read as load / arithmetic opcodes writing r0) and TotK 1.4.3 (every code
write addressed through offset register A)."""
import ast
import contextlib
import io
from pathlib import Path
import os
import re
import struct
import sys
import tempfile
from typing import Any
import unittest

from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM, CS_GRP_JUMP

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
from nso_asm import ADDI, ADRP, B, LDUR, STUR

# patch_nso.py is a command-line program whose top level loads and rewrites an image.
# Compile the scan without executing that driver, as test_jump_copies does for the recognizers.
tree = ast.parse((TOOLS / 'patch_nso.py').read_text())
scope = {}
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'cheat_written'], type_ignores=[]), str(TOOLS / 'patch_nso.py'), 'exec'), scope)
cheat_written = scope['cheat_written']

md = Cs(CS_ARCH_ARM64, CS_MODE_ARM); md.detail = True
cave_scope: dict[str, Any] = dict(struct=struct, md=md, CS_GRP_JUMP=CS_GRP_JUMP)
exec(compile(ast.Module(body=[n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and
                             n.name in ('dis', 'regset', 'b_target', 'bake_pi', 'bake_cave')], type_ignores=[]),
             str(TOOLS / 'patch_nso.py'), 'exec'), cave_scope)
bake_cave = cave_scope['bake_cave']


def store(rel, val, ofs=0, width=4):
    """0TMR00AA AAAAAAAA YYYYYYYY (YYYYYYYY): a store at main+rel + the offset register, as the tables spell it."""
    return [width << 24 | ofs << 16 | (rel >> 32) & 0xFF, rel & 0xFFFFFFFF] + ([val >> 32, val & 0xFFFFFFFF] if width == 8 else [val & 0xFFFFFFFF])


class CaveHooks(unittest.TestCase):
    def test_health_internal_conditional(self):
        # TotK 1.4.3: max health -> current health unless max is zero, then the displaced ldr x8,[x0].
        cave = {0x2BB9FEC: 0xB9401808, 0x2BB9FF0: 0x34000048, 0x2BB9FF4: 0xB9000808,
                0x2BB9FF8: 0xF9400008, 0x2BB9FFC: 0x176219F6}
        self.assertEqual(bake_cave(0x4407D0, 0x149DE607, cave), list(cave.values())[:-1])

    def test_durability_shared_call_cave(self):
        cave = {0x2BB9FE0: 0xB9401901, 0x2BB9FE4: 0xB9000901, 0x2BB9FE8: 0xD65F03C0}
        for off, word in ((0x282E58, 0x94A4DC62), (0x2829F4, 0x94A4DD7B)):
            self.assertEqual(bake_cave(off, word, cave), [0xB9401901, 0xB9000901])

    def test_internal_branch_can_target_terminal_return(self):
        for word in (0x34000040, 0x54000040, 0x36000040, B(0x1000, 0x1008)):
            cave = {0x1000: word, 0x1004: 0xD503201F, 0x1008: B(0x1008, 0x104)}
            self.assertEqual(bake_cave(0x100, B(0x100, 0x1000), cave), [word, 0xD503201F])

    def test_cave_must_be_closed_and_have_matching_return(self):
        for cave in ({}, {0x1000: 0xD503201F}, {0x1000: 0xD503201F, 0x1008: B(0x1008, 0x104)},
                     {0x1000: B(0x1000, 0x108)}, {0x1000: 0xD65F03C0},
                     {0x1000: 0x34000060, 0x1004: B(0x1004, 0x104)}):
            self.assertIsNone(bake_cave(0x100, B(0x100, 0x1000), cave))
        self.assertIsNone(bake_cave(0x100, B(0x100, 0x1000) | 0x80000000,
                                    {0x1000: B(0x1000, 0x104)}))

    def test_cave_rejects_host_sensitive_and_pc_relative_instructions(self):
        for word in (0xAA1203E0, 0x910043FF, 0xAA1E03E0, 0xD53BD040, 0xD4000001,
                     0xD61F0000, 0x94000001, 0x90000000, 0x58000000, 0):
            cave = {0x1000: word, 0x1004: B(0x1004, 0x104)}
            self.assertIsNone(bake_cave(0x100, B(0x100, 0x1000), cave), hex(word))

    def bake(self, bad_call=False):
        # Execute both real driver blocks: classify the stores, validate the hooks and emit their flag stubs.
        # The shared BL cave is in zero text padding, reached by two hooks plus one ordinary arithmetic patch.
        img = bytearray(0x8000)
        for off in (0x100, 0x104): struct.pack_into('<I', img, off, 0xB9400901)
        struct.pack_into('<I', img, 0x108, 0x1B09A148)
        words = store(0x1000, 0xB9401901) + store(0x1004, 0xD65F03C0B9000901, width=8)
        words += store(0x100, B(0x100, 0x1000) | 0x80000000)
        words += store(0x104, B(0x104, 0x1010 if bad_call else 0x1000) | 0x80000000)
        words += store(0x108, 0x1B09A3E8)
        patch = []
        env: dict[str, Any] = dict(cave_scope, os=os, re=re, cheat_written=cheat_written, IMG=img, TEXT_END=0x1000,
                   RO_OFF=0x2000, KEY_PAGE_OFF=0x4000, DATA_REFS={}, FAR=False, CHEAT_BAKE=[],
                   cuts=[], FLAGS_PAGE_OFF=0, FS_STATE_OFF=0, B=B, STUR=STUR, LDUR=LDUR, ADRP=ADRP, ADDI=ADDI,
                   here=lambda: 0x10000 + len(patch) * 4, emit=patch.append, stat=lambda _: None)
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'cheats.txt'
            src.write_text('[Durability]\n' + ' '.join(f'{w:08X}' for w in words))
            env['CHEAT_SRC'] = str(src)
            blocks: list[ast.stmt] = [n for n in tree.body if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                      and n.test.id in ('CHEAT_SRC', 'CHEAT_BAKE')]
            with contextlib.redirect_stdout(io.StringIO()):
                exec(compile(ast.Module(body=blocks, type_ignores=[]), str(TOOLS / 'patch_nso.py'), 'exec'), env)
        return env, patch

    def test_full_bake_includes_both_calls_and_preserves_link_address(self):
        env, patch = self.bake()
        self.assertEqual(len(env['CHEAT_BAKE']), 1)
        self.assertEqual(set(env['CHEAT_BAKE'][0]['bodies']), {0x100, 0x104, 0x108})
        self.assertEqual(env['IMG'][0x1000:0x100C], bytes(12))  # no executable code is written into original padding
        self.assertEqual(struct.unpack_from('<II', env['IMG'], env['FLAGS_PAGE_OFF']), (0x46434D44, 1))
        for off in (0x100, 0x104):
            site = struct.unpack_from('<I', env['IMG'], off)[0]
            start = cave_scope['b_target'](off, site)
            i = (start - 0x10000) // 4
            self.assertEqual(patch[i + 5], 0xB9400901)     # off: original instruction
            self.assertEqual(patch[i + 8], ADRP(30, start + 32, off + 4))
            self.assertEqual(patch[i + 9], ADDI(30, 30, off + 4))
            self.assertEqual(patch[i + 10:i + 12], [0xB9401901, 0xB9000901])
            self.assertEqual(cave_scope['b_target'](start + 48, patch[i + 12]), off + 4)

    def test_one_rejected_hook_does_not_leave_a_partial_baked_cheat(self):
        env, patch = self.bake(bad_call=True)
        self.assertEqual(env['CHEAT_BAKE'], [])
        self.assertEqual(patch, [])
        self.assertEqual(struct.unpack_from('<I', env['IMG'], 0x108)[0], 0x1B09A148)


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
