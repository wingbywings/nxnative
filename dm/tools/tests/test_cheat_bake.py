"""The cheat baker's register-write scan (patch_nso.py cheat_written): a word stream is decoded the way the executor
decodes it, so a stored value is never misread as an opcode, and anything the scan cannot model marks every register
(refuses baking) rather than guessing. The cases come from the tables that motivated the scan: BotW 1.9.0 (its master
stores whole ARM instructions as values, which read as load / arithmetic opcodes writing r0) and TotK 1.4.3 (every code
write addressed through offset register A)."""
import ast
import contextlib
import ctypes
import io
from pathlib import Path
import os
import platform
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


def bake_table(originals, rows, toggles='', data_refs=None):
    """Run the parser and emitter against a small synthetic module, including shared sites and restore records."""
    img, patch = bytearray(0x8000), []
    for off, word in originals.items(): struct.pack_into('<I', img, off, word)
    env = dict(cave_scope, os=os, re=re, cheat_written=cheat_written, IMG=img, TEXT_END=0x1000,
               RO_OFF=0x2000, KEY_PAGE_OFF=0x4000, DATA_REFS=data_refs or {}, FAR=False, CHEAT_BAKE=[],
               cuts=[], FLAGS_PAGE_OFF=0, FS_STATE_OFF=0, B=B, STUR=STUR, LDUR=LDUR, ADRP=ADRP, ADDI=ADDI,
               here=lambda: 0x10000 + len(patch) * 4, emit=patch.append, stat=lambda _: None)
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / 'cheats.txt'
        src.write_text(''.join(f'[{name}]\n' + ' '.join(f'{w:08X}' for w in words) + '\n' for name, words in rows))
        (Path(d) / 'toggles.txt').write_text(toggles)
        env['CHEAT_SRC'] = str(src)
        blocks: list[ast.stmt] = [n for n in tree.body if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                  and n.test.id in ('CHEAT_SRC', 'CHEAT_BAKE')]
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(ast.Module(body=blocks, type_ignores=[]), str(TOOLS / 'patch_nso.py'), 'exec'), env)
    return env, patch


class SharedSites(unittest.TestCase):
    def test_variants_restore_alias_and_disabled_build_switches(self):
        env, _ = bake_table({0x100: 0x52800020, 0x104: 0x52800040}, [
            ('First', store(0x100, 0x52800060) + store(0x104, 0x52800080)),
            ('Second', store(0x100, 0x528000A0)), ('Alias', store(0x100, 0x528000A0)),
            ('Restore', store(0x100, 0x52800020))], '[First] off\n[Second] off\n')
        self.assertEqual([c['name'] for c in env['CHEAT_BAKE']], ['First', 'Second', 'Alias', 'Restore'])
        hdr = env['IMG'][env['FLAGS_PAGE_OFF']:]
        self.assertEqual(struct.unpack_from('<III', hdr), (0x46434D44, 4, 2))
        self.assertEqual(struct.unpack_from('<I', hdr, 0x20 + 2 * 0x48 + 4)[0], 2 << 8)
        self.assertEqual(struct.unpack_from('<I', hdr, 0x20 + 3 * 0x48 + 4)[0], 1)
        self.assertEqual(struct.unpack_from('<Q', hdr, 0x1600 + 3 * 8)[0], 7)
        shared = [c for c in env['cuts'] if c[0] == 0x100]
        self.assertEqual(len(shared), 3)
        self.assertEqual(len({c[2] for c in shared}), 1)  # one dispatcher, all associations retained

    def test_unsupported_site_drops_whole_entry_but_not_other_variants(self):
        env, _ = bake_table({0x100: 0x52800020, 0x104: 0x90000000}, [
            ('Partial', store(0x100, 0x52800060) + store(0x104, 0xD503201F)),
            ('Complete', store(0x100, 0x52800080))])
        self.assertEqual([c['name'] for c in env['CHEAT_BAKE']], ['Complete'])
        self.assertEqual(struct.unpack_from('<I', env['IMG'], 0x104)[0], 0x90000000)

    def test_mixed_data_and_code_cannot_suppress_runtime_half(self):
        env, patch = bake_table({0x100: 0x52800020, 0x104: 0x52800040}, [
            ('Mixed', store(0x100, 0x52800060) + store(0x104, 0x52800080))], data_refs={0x104: True})
        self.assertEqual(env['CHEAT_BAKE'], [])
        self.assertEqual(patch, [])

    def test_key_else_and_unconditional_restore_prefix(self):
        for words in ([0x80000002] + store(0x100, 0x52800060) + [0x21000000] + store(0x100, 0x52800020) + [0x20000000],
                      store(0x100, 0x52800020) + [0x80000002] + store(0x100, 0x52800060) + [0x20000000]):
            env, _ = bake_table({0x100: 0x52800020}, [('Hold', words)])
            self.assertTrue(env['CHEAT_BAKE'][0]['has_else'])
            self.assertEqual(struct.unpack_from('<II', env['IMG'], env['FLAGS_PAGE_OFF'] + 0x20), (2, 2))

    def test_nonuniform_key_bodies_are_not_partially_baked(self):
        for words in ([0x80000002, 0x80000001] + store(0x100, 0x52800060) + [0x20000000, 0x20000000],
                      [0x80000002] + store(0x100, 0x52800060) + [0x20000000] + store(0x104, 0x52800080),
                      [0x80000002] + store(0x100, 0x52800060) + [0x21000000] + store(0x104, 0x52800040) + [0x20000000],
                      store(0x104, 0x52800080) + [0x80000002] + store(0x100, 0x52800060) + [0x20000000]):
            env, patch = bake_table({0x100: 0x52800020, 0x104: 0x52800040}, [('Complex', words)])
            self.assertEqual(env['CHEAT_BAKE'], [])
            self.assertEqual(patch, [])

    def test_u64_restore_checks_both_original_words(self):
        words = [0x80000002] + store(0x100, 0x5280006052800080, width=8) + [0x21000000]
        words += store(0x100, 0x5280000052800020, width=8) + [0x20000000]
        with self.assertRaisesRegex(AssertionError, 'the table is for another build'):
            bake_table({0x100: 0x52800020, 0x104: 0x52800040}, [('Wrong upper half', words)])

    def test_branch_to_external_image_or_guest_x18_requires_other_rewriting(self):
        for word in (B(0x100, 0x2000), 0x34000112, 0x36000112):
            env, patch = bake_table({0x100: 0x52800020}, [('Unsupported branch', store(0x100, word))])
            self.assertEqual(env['CHEAT_BAKE'], [])
            self.assertEqual(patch, [])


@unittest.skipUnless(sys.platform == 'darwin' and platform.machine() == 'arm64', 'native ARM64 dispatcher execution on macOS')
class NativeDispatch(unittest.TestCase):
    def execute(self, originals, rows, states, arg=0, nzcv=0):
        # A synthetic caller preserves its ABI frame. Branch destinations record x9, NZCV and the link register
        # before returning to it; this catches flag/register clobbers as well as incorrect taken/fallthrough exits.
        originals = dict(originals)
        originals.update({0x80: 0xA9BF7BFD, 0x84: 0xD2824689, 0x88: B(0x88, 0x100) | 0x80000000,
                          0x8C: 0xA8C17BFD, 0x90: 0xD65F03C0,
                          0x180: 0xF9000029, 0x184: 0xD53B420A, 0x188: 0xF900042A,
                          0x18C: 0xF900083E, 0x190: B(0x190, 0x8C)})
        # Set NZCV from x2 just before the call, without affecting the seeded scratch register.
        originals[0x88] = 0xD51B4202
        originals[0x8C] = B(0x8C, 0x100) | 0x80000000
        originals[0x90], originals[0x94], originals[0x190] = 0xA8C17BFD, 0xD65F03C0, B(0x190, 0x90)
        env, patch = bake_table(originals, rows)
        img = env['IMG'] + struct.pack('<' + 'I' * len(patch), *patch)
        self.assertEqual(len(env['IMG']), 0x10000)
        for i, state in enumerate(states): struct.pack_into('<I', img, env['FLAGS_PAGE_OFF'] + 0x1400 + 4 * i, state)
        libc = ctypes.CDLL(None)
        libc.mmap.restype = ctypes.c_void_p
        libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_longlong]
        libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        libc.sys_icache_invalidate.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        size = 0x20000
        addr = libc.mmap(None, size, 7, 0x1002 | 0x800, -1, 0)  # MAP_ANON | MAP_PRIVATE | MAP_JIT
        self.assertNotEqual(addr, ctypes.c_void_p(-1).value)
        try:
            libc.pthread_jit_write_protect_np(0)
            ctypes.memmove(addr, bytes(img), len(img))
            libc.sys_icache_invalidate(addr, size)
            libc.pthread_jit_write_protect_np(1)
            observed = (ctypes.c_uint64 * 3)()
            call = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64)(addr + 0x80)
            value = call(arg, observed, nzcv)
            self.assertEqual(observed[0], 0x1234)
            self.assertEqual(observed[1], nzcv)
            return value, observed[2] - addr
        finally:
            libc.munmap(addr, size)

    def test_shared_variants_and_released_else_priority(self):
        originals = {0x100: 0x52800020, 0x104: B(0x104, 0x180)}
        rows = [('First', store(0x100, 0x52800040)),
                ('Hold', [0x80000002] + store(0x100, 0x52800060) + [0x21000000] + store(0x100, 0x52800020) + [0x20000000])]
        for states, expected in (([0, 0], 1), ([1, 0], 2), ([0, 1], 3), ([1, 1], 3), ([1, 2], 1)):
            with self.subTest(states=states):
                self.assertEqual(self.execute(originals, rows, states, nzcv=0xA0000000), (expected, 0x90))

    def test_three_hold_speeds_can_be_disabled_and_reselected(self):
        originals = {0x100: 0x52800020, 0x104: B(0x104, 0x180)}
        rows = [(f'Speed {speed}', [0x80000002] + store(0x100, 0x52800000 | speed << 5) +
                 [0x21000000] + store(0x100, 0x52800020) + [0x20000000]) for speed in (2, 3, 10)]
        # Enable 10x, release B, disable it, then select 2x and 3x separately; finally restore normal speed.
        for states, expected in (([0, 0, 1], 10), ([0, 0, 2], 1), ([0, 0, 0], 1),
                                 ([1, 0, 0], 2), ([2, 0, 0], 1), ([0, 0, 0], 1),
                                 ([0, 1, 0], 3), ([0, 2, 0], 1), ([0, 0, 0], 1)):
            with self.subTest(states=states):
                self.assertEqual(self.execute(originals, rows, states), (expected, 0x90))

    def test_conditional_originals_taken_and_fallthrough(self):
        # Forward/backward offsets, zero/nonzero, bit tests, and condition flags, both 32 and 64 bit forms.
        for source_word, arg, nzcv, taken in ((0x34000100, 0, 0, True), (0x34000100, 1, 0, False),
                (0xB5000100, 1, 0, True), (0xB5000100, 0, 0, False),
                (0x36000100, 0, 0, True), (0x37000100, 1, 0, True),
                (0x5400010C, 0, 0, True), (0x5400010C, 0, 0x40000000, False),
                (0x34FFFF00, 0, 0, True)):
            # The last word encodes cbz w0,0xe0 (a negative offset).
            target = 0xE0 if source_word == 0x34FFFF00 else 0x120
            originals = {0x100: source_word, 0x104: 0x52800040, 0x108: B(0x108, 0x180),
                         target: 0x52800060, target + 4: B(target + 4, 0x180)}
            for enabled in (0, 1):
                with self.subTest(word=hex(source_word), arg=arg, enabled=enabled):
                    result = self.execute(originals, [('Nop', store(0x100, 0xD503201F))], [enabled], arg, nzcv)
                    self.assertEqual(result, (3 if taken and not enabled else 2, 0x90))

    def test_branch_replacement_and_original_call_link(self):
        originals = {0x100: B(0x100, 0x120) | 0x80000000, 0x104: B(0x104, 0x180),
                     0x120: 0x528000E0, 0x124: 0xD65F03C0, 0x140: 0x52800120, 0x144: B(0x144, 0x180)}
        rows = [('Branch', store(0x100, B(0x100, 0x140)))]
        self.assertEqual(self.execute(originals, rows, [0]), (7, 0x104))
        self.assertEqual(self.execute(originals, rows, [1]), (9, 0x90))


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
