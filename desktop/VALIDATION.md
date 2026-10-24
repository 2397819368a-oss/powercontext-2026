# Desktop validation

Desktop is an unsigned internal Windows and macOS preview. Passing CI does not establish release qualification.

## Reproducible checks

Run from the repository root:

```sh
pnpm --dir desktop install --frozen-lockfile
pnpm --dir desktop lint
pnpm --dir desktop typecheck
pnpm --dir desktop test
pnpm --dir desktop build
pnpm --dir desktop ipc:check
cargo fmt --manifest-path desktop/src-tauri/Cargo.toml --check
cargo clippy --locked --manifest-path desktop/src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml
```

[Desktop CI](../.github/workflows/desktop.yml) also builds a Server wheel, runs real Server and CLI tests, checks Windows credentials and process containment, builds the installer, and exercises installation, the installed UI and uninstallation. Consult the PR checks for the exact commit being reviewed. Reports, logs and screenshots are generated under ignored `.artifacts/` and uploaded as Actions artifacts; do not copy them into the repository.

## Compatibility scope

[compatibility.json](src-tauri/src/connections/compatibility.json) preserves the recorded Server sources, normalized OpenAPI contract digests and qualification-wheel digests for the historical profiles below. The current contract is qualified by `sqlite-stats-1da99747-v1` below; ordinary builds require explicitly selecting it and checking the connection before business operations become available. The `sqlite-atomic-caf2a35a-v1`, `sqlite-atomic-683e2db0-v1` and `sqlite-atomic-cab054db-v1` records qualify their corresponding Server source deployments and do not match this bundled contract. Historical records are disabled in the selector, and saved unavailable selections remain visible until explicitly replaced. CI builds have their own artifact identities and do not change product qualification.

The real Server harness covers SQLite with anonymous loopback, static Bearer, HTTPS with explicit CA/base path, and injected-provider/enforced access. It exercises save/search/exact reads, identity changes, binding revocation, ambiguous writes without replay and Scope pagination. Installed UI checks cover repeated launches preserving the existing editor, late committed-save responses preserving a new Scope draft, explicit connection/Scope selection, clipboard copies, connection isolation, draft cancellation, byte limits and independent Server survival after Desktop exit.

Selecting a compatibility profile does not attest the remote binary identity. Existing `sqlite-1.1.1-v1` / `sqlite-63f918b7-v1` / `sqlite-ab43e3a7-v1` / `sqlite-f1089f4e-v1` / `sqlite-6e237568-v1` / `sqlite-legacy-58f7f4f6-v1` selections require explicitly choosing an available matching profile and rechecking the connection. Desktop reads memories only through Atomic Memory references, so Servers that answer only with Memory entry citations are not qualified. CLI diagnostics separately verify the registered executable path and digest, then validate the diagnostic response schema. The observed version is optional report information; a registration does not require a version.

### Current-Scope statistics qualification

`sqlite-stats-1da99747-v1` records Server commit
`1da997476651c2850ff7141e2b7cd512d0152a90`, contract digest
`5878999ce9e239d6642e4da858074b281ed465629432c578b3584eec47cbce6c`, and wheel
`powercontext-1.4.1.dev139+g1da997476.d20261024-py3-none-any.whl` with SHA-256
`4eda168954bb5950aa4f0bbc0d534b2b2bdb15ff43f97de7df279963c46c70a8`.
All 457 Python runtime files in that wheel match the checkout's runtime sources;
their sorted `path sha256` manifest, newline terminated, hashes to
`49fe8325db1955ee250c3a76a8efad3d87fc393dc1007a95817524f38ded41ad`.
The Desktop adapter and harness had local changes during this run; the Server
runtime, OpenAPI contract and dependency lock matched the recorded commit.

On macOS 26.7 ARM64, Python 3.12.13 and Rust 1.95.0, the native `ConnectionManager`
and `ServerApi` passed the isolated built-wheel harness in anonymous loopback,
Bearer loopback, HTTPS with explicit CA/base path, and enforced-provider modes.
The eleven recorded operations include `get_stats`. Statistics checks exercise
the exact Scope, genuine empty inventory, `today`/`7d`/`30d` UTC periods,
persisted Memory inventory, no-model zero usage, and same-Principal permission
revocation without selection fallback. Partial provider usage is explicitly
seeded through the real Runtime recorder: two generation requests, unknown input
tokens and nine output tokens survive persistence, aggregation and native parsing.
This seeded input does not establish that an actual model provider emitted it.

Synthetic native tests separately cover cancellation, changed Scope, independent
Memory cancellation, response bounds, and invalid response selection/period.
The harness reports are generated under `.artifacts/`; reproduce them with
`fixture_qualification.py`, the `server_probe` example and `real_server.py`.
These results qualify the observed Server protocol.

The bundled macOS ARM64 debug app was also exercised against two isolated real
Servers on this host, using an isolated application identifier and an 800 × 600
window. The build used Desktop `34c61c5f` plus the main-window zoom permission
in this PR; its executable SHA-256 is
`43d0d149d3194739171ffb70a57736280c14d2cd3d63fe47001c8f2b3381065d`.
Observed results include explicit activation after restart, empty inventory,
Chinese and English partial-token displays, clearing on Scope/connection changes,
and the second Server's independent one-entry inventory with zero usage.
Command+0 followed by five Command+= steps exercised 200% webview zoom, including
a successful statistics reload. The zoom IPC permission is restricted to the
trusted local main window. This was a bundled debug-app check, not installer,
IME, screen-reader, signing or complete accessibility acceptance.

`installed_statistics.py` extends the Windows installed WebDriver journey with
independent real `/v1/stats` table comparisons, all three UTC periods, bilingual
unknown-token handling, and Scope/connection isolation. Its fixture and Python
checks ran locally; the Windows UI journey requires actual Windows execution.
Hosted workflows at `34c61c5f` required maintainer approval (`action_required`),
so that head had no Windows acceptance result. Intel and release gates remain
separate.

### Current Server protocol qualification

#### Server d9a78567 protocol qualification

The historical contract is qualified by `sqlite-atomic-d9a78567-v1`. The record identifies Server
sources at `d9a785675dedb72cd72a386b346e590f4c067ac0` and normalized OpenAPI digest
`a6758fd35296abefb55ab86b152584a369d5f2deedfee7a3f9822e16ad1edc25`.
The qualification wheel is `powercontext-1.4.1.dev69+ga200d6094.d20261022-py3-none-any.whl`,
SHA-256 `cbd2b544b12c781f4e331e1f0bcc9c9203392dea84f7d2f4491dce9bfd45c7f4`.
All 537 packaged runtime files match the recorded Server commit byte for byte; the OpenAPI
contract, project configuration and lock also match. The sorted runtime source-manifest digest
is `4ceb111b390b7558c955d8fef0c87f96355edc522b767340f7adac963880ba70`
(one `wheel/path sha256` entry per line, with a final newline).

The existing real-Server harness passed against the isolated wheel and SQLite in all four modes:
anonymous loopback, static Bearer, HTTPS with an explicit CA and base path, and an injected
provider with enforced access. The ten recorded operations cover Scope selection and pagination,
Atomic Memory save/search/exact revision reads, identity changes, same-identity revocation,
committed writes with lost responses without replay, and independent Server survival after the
client exits.

Qualification ran on Linux x86_64 with Python 3.12.13 and Rust 1.95.0. The standalone Rust client
compiled the unmodified product connection, transport, credential and error modules, plus the
existing `server_probe.rs`, with dependency versions from the product Cargo lock. The Tauri GUI
shell was excluded. This establishes the stated Server protocol compatibility; it does not
qualify Linux Desktop support, native macOS/Windows packaging, credential stores, or installed
UI workflows. Historical qualification records retain their exact identities and evidence.

#### Server cab054db protocol qualification

The corresponding historical preview contract is qualified by `sqlite-atomic-cab054db-v1`. It identifies Server sources
at `cab054dbf6d86e2311b079388e5158178257def2` and normalized OpenAPI digest
`042842aee551aa6930c90ce99724f88ebed2547ed42e5f02bc5d8191163b907b` (release v1.4.0 changes the OpenAPI
version only). The qualification wheel is
`powercontext-1.3.1.dev114+g9243caa3a-py3-none-any.whl`, SHA-256
`042c28f85caae59bc6df3cdec9a78fad50f67cd2492b5bd229dc740e2af8f8d9`. It was built at Desktop merge
commit `9243caa3a8ed0f8683c87681c71269571ff4768b`; Server runtime sources are unchanged since
`cab054db`. All 517 packaged runtime files match the recorded Server commit byte for byte; the
OpenAPI contract, project configuration and lock also match. The sorted runtime source-manifest
digest is `d8f1257b59192feab62f82d76355457719e1e45ea2cf3103f5296d32e658893d`
(one `wheel/path sha256` entry per line, with a final newline).

The native `ConnectionManager` exercised the isolated wheel and SQLite in anonymous loopback,
static Bearer, HTTPS with explicit CA/base path, and injected-provider/enforced-access modes.
All four passed on macOS 26.5.2 ARM64, Python 3.12.13 and Rust 1.95.0. The ten recorded operations
cover Atomic Memory save/search/exact revision reads, Scope pagination, identity changes,
revocation, committed writes with lost responses without replay, and independent Server survival
after client exit. This establishes protocol compatibility for these sources and this wheel.
The tested native client source includes the macOS host support and the bounded diagnostic
pipe collection described in the [security boundary](SECURITY.md). Windows installer acceptance,
Intel execution, signing and accessibility require their own checks. Historical records retain
their original source, contract and wheel identities.

#### Server 683e2db0 protocol qualification

The bundled contract of the previous preview build is qualified by `sqlite-atomic-683e2db0-v1`. It
identifies Server sources
at `683e2db081fc58a69f6527c1916ae77cdeeeb371` and normalized OpenAPI digest
`dae5feab227a6c16a8199455a27c82b444c9c17b43bec7de0fca4790c7bed14f`. The qualification wheel is
`powercontext-1.3.1.dev105+g7f8d5dfdf-py3-none-any.whl`, SHA-256
`dd81cfadb5ce6999873eab25ff5318a6a794987597d83daeb4f0eb90618e95c1`. It was built at Desktop commit
`7f8d5dfdf74f39d7c51af908e2c1ebe3e73205b9`. All 517 packaged runtime files match the recorded Server
commit byte for byte; the OpenAPI contract, project configuration and lock also match.
The sorted runtime source-manifest digest is
`0d750d72f9933631d16833dd7f69be3dd6cffcf083035d5457b8f2b313b1e5e9` (one `wheel/path sha256` entry per line, with a final newline).

The native `ConnectionManager` exercised the isolated wheel and SQLite in anonymous loopback,
static Bearer, HTTPS with explicit CA/base path, and injected-provider/enforced-access modes.
All four passed on macOS 26.5.2 ARM64, Python 3.12.13 and Rust 1.95.0. The ten recorded operations
cover Atomic Memory save/search/exact revision reads, Scope pagination, identity changes,
revocation, committed writes with lost responses without replay, and independent Server survival
after client exit. This establishes protocol compatibility for these sources and this wheel.
Windows installer acceptance, Intel execution, signing and accessibility require their own checks.
Historical records retain their original source, contract and wheel identities.

#### Server caf2a35a protocol qualification

`sqlite-atomic-caf2a35a-v1` records Server commit `caf2a35aa5c626e013584a2c3a37dcd94e0d49bf`,
normalized OpenAPI contract digest `e43c7034b6930688c350c68af5e7bd6083b123783b92d0a50eebbebd45aa7da7`,
and wheel digest `4ac43be2f914f8075f97b896b9e2773fcfeff0b5fe354241c6d02d44dbb4a955`.
The qualification wheel is `powercontext-1.3.1.dev89+gcaf2a35aa.d20261018-py3-none-any.whl`.
All 512 packaged runtime files match the recorded commit byte for byte. The sorted runtime
source-manifest digest (one `wheel/path sha256` entry per line, with a final newline) is
`a5c0fba7746b38b0f69db2a07689353f98f2f0526f9315684687704d0aeb3ee2`.
The wheel was built before Desktop-only commits; those commits do not change Server sources.

The native real-Server harness passed on macOS 26.5.2 ARM64, Python 3.14.7 and Rust 1.95.0.
It used the isolated wheel and SQLite in four modes: anonymous loopback, static Bearer,
HTTPS with an explicit CA and base path, and an injected authentication provider with enforced access.
It exercised the ten recorded operations, including Atomic Memory save/search/exact revision reads,
identity changes, revocation with an unchanged identity, Scope pagination, committed writes with lost
responses without replay, and independent Server survival after the native client exited.
Fixture certificates include Authority Key Identifier extensions for strict X.509 verification.
The tested native client source includes the macOS host support and strict fixture-certificate change.

This is protocol qualification of that source and wheel, not binary attestation of arbitrary remote
Servers. Users must check their deployed source before selecting the profile. Windows and Intel
native checks, installed UI workflows, signing and notarization require their own evidence.
Reproduce with `uv build --wheel --out-dir desktop/.artifacts/server-wheel`,
`fixture_qualification.py --server-commit <the recorded Server commit>`,
`cargo build --locked --manifest-path desktop/src-tauri/Cargo.toml --example server_probe`,
and `uv run python desktop/tests/real_server.py`. New builds have their own wheel identities.

### Historical macOS installed application workflow

This observation was recorded in Desktop commit `984e8c60fed262f60e1054c17c8b7d84489af999`,
whose bundled OpenAPI digest was `e43c7034b6930688c350c68af5e7bd6083b123783b92d0a50eebbebd45aa7da7`,
matching `sqlite-atomic-caf2a35a-v1`. The observed app is identified by the executable SHA-256 below;
this record does not identify its exact build source revision. This is historical evidence, not a
procedure or native UI qualification for the current bundled contract.

The current `sqlite-atomic-d9a78567-v1` record has non-GUI protocol evidence on Linux x86_64 only.
Native macOS save/search/exact-read and clipboard acceptance against that record remains unverified.

The normal release app (without `ci-fixtures`) was operated on macOS 26.5.2 ARM64 against the
wheel recorded under [Server caf2a35a protocol qualification](#server-caf2a35a-protocol-qualification)
on a dedicated loopback Server and SQLite workspace. The UI saved its
connection profile, explicitly selected `sqlite-atomic-caf2a35a-v1`, checked/activated the connection,
searched by Scope title and selected its exact ID. It saved a multiline Chinese/English note,
found that note through FTS, and read its exact Atomic Memory revision. Both copy buttons were
verified by pasting the actual clipboard into the app editor: full text remained unchanged and
reference JSON retained `family`, `artifact_id` and `revision`. An independent HTTP read returned
the same persisted exact text. Test paste-back drafts were cleared without saving duplicates.
The saved connection and qualification choice survived application restart. After read-only DMG
verification, the app was copied to a separate directory and launched from that copy; its executable
SHA-256 matched the built app (`ba8ee45b5895572737a8b639535dee4a5725e54c0eda6a10cc49169974f9441f`).
The copied app reconnected, selected the Scope, searched and read the original exact revision.
The independent demo Server remained alive after Desktop exited, and its persisted note also
survived restarting the Server.

This establishes those workflows on the observed host and build. It does not establish actual IME
composition (Chinese text was pasted), screen-reader behavior, Intel/Windows execution, Gatekeeper
acceptance, signing or notarization.

### Atomic Memory protocol qualification

`sqlite-atomic-f702c041-v1` identifies Server sources at `f702c0418d69e3072e1ec2fa657e0df81e8e51b4`, contract digest `5d8bbad951234b05e3174f1b9113e0ffe066ac968abbe53ac2456b2375e406d3`, and qualification-wheel digest `4fe64b868a2e214316a088574d8d4023d9b9679ec42029e2d13502121e6aade9`. The wheel version is `1.2.1.dev78+g77dc6f73b.d20261009`. It was captured at HEAD `77dc6f73b86678221d34a14e2de61dade0ff9db3` with staged tree `d2e9d703877ea707d78191ce0c358112fd69f65f` and a dirty working tree. That tree became the recorded Server commit; all 460 runtime files in the wheel match its sources byte for byte. The runtime source-manifest digest is `e26a4c4581f32b855689e3e0db26b48239f2f6758ab594addc9efc047978390d` (sorted wheel runtime paths and content digests). The OpenAPI contract, project configuration and lock remained unchanged between capture and commit. CI builds have their own wheel identities.

The native `ConnectionManager` exercised the actual isolated built-wheel SQLite Server in four modes: anonymous loopback, static Bearer, HTTPS with explicit CA/base path, and injected-provider/enforced access. All four passed on macOS 26.2 ARM64 with Rust 1.95.0 and Python 3.12.7. Checks covered Atomic Memory save/search/exact Artifact revision reads, identity changes, binding revocation, ambiguous writes without replay, Scope pagination and independent Server survival after client exit. The catalog records the ten operations exercised, including `get_artifact_revision`. The native harness candidate remains separate from the product catalog.

These results establish the stated Server protocol compatibility. Windows credentials, process containment, registered-executable checks and installed-package acceptance for this Atomic profile require their actual Windows CI checks. This protocol record does not establish platform or release acceptance.

### Historical protocol qualifications

`sqlite-6e237568-v1` records the original Server source, contract digest and qualification-wheel digest. Its qualification wheel was built from checkout `ea30fd5c`; its backend sources, OpenAPI contract, project configuration and lock matched the recorded Server commit. The immutable catalog preserves its identities and original ten operations.

`sqlite-legacy-58f7f4f6-v1` identifies Server sources at `58f7f4f6cbadd698bac35b3fca019d9f48f55b81`, contract digest `9f4ba7b35ecd3b3639af4e648aff84b713a7f30b51d5a4c30c8809aeb8dc8024`, and qualification-wheel digest `7203cf1a98cb9dfd8631e142f7a0f5e74c39845da4fb7d862117a49fa1ef38af`. Its original ten Desktop operations and Legacy Memory protocol were exercised by the native `ConnectionManager` against the isolated built-wheel SQLite Server in all four harness modes: anonymous loopback, Bearer, HTTPS with explicit CA/base path, and injected-provider/enforced access. These checks cover save/search/exact historical citation reads, identity changes, revocation, ambiguous writes without replay, and Scope pagination. The original `sqlite-6e237568-v1` record retains its identities and operations. The Base Legacy qualification requires explicitly selecting a profile whose recorded contract matches the bundled Desktop contract and rechecking the connection.

This qualification used macOS 26.2 ARM64, Rust 1.95.0 and Python 3.12.7. An external system Python launcher was used for native process creation because this environment can terminate native subprocesses before their first request. The wheel, native client, fixtures and business assertions were preserved. The real CLI's fixed version, service-status and integration commands were also exercised with an isolated home and PATH; their actual outputs passed the native projections. These checks establish the stated SQLite compatibility and CLI response handling. Windows registered-executable checks, credentials, process containment and installed-package acceptance require their actual Windows CI checks. No remote legacy deployment was tested in this qualification.

`sqlite-atomic-7bd5b85c-v1` identifies Server sources at `7bd5b85c154c284d0ad550de31ecf8aa629d8ccf`, contract digest `0807df984ba706b505d4fff0cb849d4566222f10572d91a5e03399b833d1fe4f`, and qualification-wheel digest `057eca5de29961684bfea8ea8aece7c133b3d8b46a024db69a944519bc5b2746`. Its native `ConnectionManager` was exercised against the isolated built-wheel SQLite Server in all four harness modes: anonymous loopback, Bearer, HTTPS with explicit CA/base path, and injected-provider/enforced access. These checks cover Atomic writes, all returned references, search, immutable revision reads, identity changes, revocation, ambiguous writes without replay, and Scope pagination. These are historical results for that qualified build; this Desktop build reads only Atomic Memory references and does not retain the Legacy citation protocol.

The Atomic qualification run used macOS 26.2 ARM64, Rust 1.95.0 and Python 3.12.7. An external launcher routed native process creation through system Python 3.9.6 because the same signed native executable exited with SIGKILL when launched by the Conda Python parent before its first request. The wheel, native client, harness fixtures and business assertions were unchanged by that launcher. This establishes the stated SQLite compatibility; Windows IPC, vault, process containment and installed-package acceptance require their actual Windows CI checks. No legacy remote deployment was tested in the Atomic qualification run.

## CI fixture 的独立资格

CI fixture 使用独立资格记录，不会修改历史生产资格。原生 HTTP 测试显式注入 synthetic profile；真实 Server 和安装验收使用 `fixture_qualification.py` 从当前合同、Server 提交和构建 wheel 生成的 `.artifacts/ci-compatibility.json`，读取时重新核对合同、操作集合和 wheel 哈希。

只有显式启用 `ci-fixtures` 的测试安装包会包含这个 profile。普通构建仍只使用历史资格记录，并保留合同不匹配时的 fail-closed 行为。CI fixture 和其安装包不能作为生产发布资格证据。

```sh
uv build --wheel --out-dir desktop/.artifacts/server-wheel
DESKTOP_FIXTURE_COMMIT=$(git rev-parse HEAD)
uv run python desktop/tests/fixture_qualification.py --server-commit "$DESKTOP_FIXTURE_COMMIT"
pnpm --dir desktop desktop:build:windows:ci
```

## Remaining release qualification

- Clean standard-user Windows 11, including absent/present WebView2 and bootstrap recovery.
- Publisher signing and release ownership.
- Actual IME, screen reader, high contrast and complete keyboard accessibility.
- Notification cold activation, independent service login/reboot behavior and a named Agent host's capture/recall workflow.
- Maintainer ownership and approved performance budgets.

Hosted Windows CI and developer-machine checks do not replace these platform and product gates. This preview does not close #1654 or #1428.

## macOS native validation

Run on each target architecture with the pinned tools listed in the [macOS build prerequisites](README.md#build-on-macos),
including JDK 11 or newer with `java` on `PATH`. Both `lint` and `build` invoke the Java OpenAPI
Generator through `generate:check`; its first run may require downloading the pinned JAR from
Maven Central. A preinstalled JDK on a CI image does not replace this local prerequisite.

```sh
cd desktop
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo clippy --locked --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --locked --manifest-path src-tauri/Cargo.toml
pnpm ipc:check
cargo run --locked --manifest-path src-tauri/Cargo.toml --example credential_probe
pnpm desktop:build
open "src-tauri/target/release/bundle/macos/PowerContext Desktop Preview.app"
```

Check that the Home page renders, Settings shows the native host ready, and a missing diagnostic
registration produces the missing-CLI state. The synthetic native Keychain probe verifies persistence
across processes and removes its own entry. Native process tests exercise output limits, timeout,
cancellation and descendant termination. The registered-launcher test executes a real synthetic CLI
and checks digest rejection and allowlisted projection; it does not qualify a real CLI installation.

Use a disposable Server and Scope for installed save/search/read/copy checks. Unit tests do not prove
those installed workflows. Validate actual Chinese IME, accessibility, Gatekeeper, signing and
notarization independently before distributing outside the internal preview.
