# Packing

## Build tools

### Homebrew .NET is on PATH but apphosts cannot find the runtime

- **Symptom:** the first extraction stops with `nx_extract exit code 131` and `.NET location: Not found`, even though `dotnet` is on `PATH`. The shader translator can fail the same way.
- **Cause:** standalone .NET apphosts search runtime roots and registered installations, not `PATH` ([.NET runtime discovery](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-environment-variables#dotnet_root-dotnet_rootx86-dotnet_root_x86-dotnet_root_x64)). Homebrew's formula installs the runtime under `libexec` and its `bin/dotnet` can be a wrapper that sets `DOTNET_ROOT` only for its own child ([Homebrew caveat](https://formulae.brew.sh/formula/dotnet)). Resolving that wrapper's symlink gives `bin`, not the runtime root.
- **Fix:** `scripts/build_ipa_from_nsp.py` `configure_dotnet` derives the root from the `Microsoft.NETCore.App` entry in `dotnet --list-runtimes`, using the build's environment, before any extraction or survey. It exports `DOTNET_ROOT` both to that environment and to `os.environ`, for tools launched with an explicit environment and those that inherit it. Caller-supplied roots, including architecture-specific overrides, retain precedence. A failed or malformed probe leaves the environment unchanged rather than guessing the wrapper's directory.
- **Found on:** [Balatro](../../dm/games/0100CD801CE5E000/README.md), first extraction with Homebrew .NET ([PR #284](https://github.com/seedds/nxnative/pull/284)). Applies to every game's build on that installation setup.

## IPA archives

### A streamed file changes size and the IPA fails its CRC check

- **Symptom:** packing succeeds, but `unzip -t` or Python's `ZipFile.testzip()` reports a bad CRC for a large deflated file.
- **Cause:** `pack_ipa.py` wrote the uncompressed size collected before reading. Its streaming check compared the output size only for stored files, so a compressible file that grew or shrank still published an archive with the old size.
- **Fix:** count uncompressed bytes as they are read and compare them with the listed size before completing either streamed method. A mismatch stops the pack and removes the partial archive, preserving the previous IPA and its reuse index. Regression tests cover growth and shrinkage with both deflate and store.
- **Found on:** synthetic IPA inputs while reviewing [PR #279](https://github.com/seedds/nxnative/pull/279); no game files required.

## Compressed inputs

### NSZ decompression rejects `--keys`

- **Symptom:** `decompress NSZ` stops immediately with `nsz: error: unrecognized arguments: --keys …`.
- **Cause:** `nsz` 4.6.1 has no `--keys` argument. It loads `keys.txt` beside `sys.argv[0]`, or falls back to `~/.switch/prod.keys`, while importing its modules, before parsing arguments ([4.6.1 key loader](https://github.com/nicoboss/nsz/blob/4.6.1/nsz/nut/Keys.py)). Newer upstream code supports `--keys`; staging `keys.txt` also supports 4.6.1.
- **Fix:** `scripts/build_ipa_from_nsp.py` `decompress_nsz` links the file selected by `-k` as `keys.txt` in the conversion's temporary folder and runs `nsz -D` there, with `sys.argv[0]` set to `nsz` before import. Input and output paths are absolute. The temporary folder and link are removed on success or failure; the user's default keys are untouched.
- **Found on:** [Super Mario Odyssey](../../dm/games/0100000000010000/README.md), converting its v0 NSZ. Applies to NSZ and XCZ inputs for every game.

## The app and its files

### Documents/cheats starts empty on a fresh install

- **Symptom:** an empty in-game cheat menu beside the missing baked patches ([Baking stores through an offset register](rewriter.md#baking-stores-through-an-offset-register)): the cheats the rewriter cannot bake (pointer chains and other store shapes) had no file to run from on the phone, and a table had to be copied by hand into `Documents/cheats` through the Files app.
- **Cause:** the NSP script used the game folder's `cheats/<main build ID>.txt` only for baking (`DM_CHEAT_FILE` to the rewriter); nothing packed the table into the app, and iOS gives a freshly installed app an empty `Documents`.
- **Fix:** the NSP script passes the packed build's table to `build_ipa.sh` (`DM_CHEAT_TABLE`), which puts it at `DmIOS.app/dm/cheats/`; `main.m` copies every `.txt` of it into `Documents/cheats` at boot when a file of that name is not already there (the player's files, edits and `toggles.txt` win). The Mac run command sets `DM_CHEATS_DIR` to the game folder's `cheats/`, where the engine then also writes `toggles.txt`.
- **Found on:** [The Legend of Zelda: Tears of the Kingdom](../../dm/games/0100F2C0115B6000/README.md) 1.4.3.
