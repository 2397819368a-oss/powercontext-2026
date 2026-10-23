# Desktop validation

Desktop is an unsigned internal Windows preview. Passing CI does not establish release qualification.

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

[compatibility.json](src-tauri/src/connections/compatibility.json) preserves the recorded Server sources, normalized OpenAPI contract digests and qualification-wheel digests for the historical profiles below. No product record currently matches this build’s bundled contract; ordinary builds keep business operations unavailable until an independently verified matching profile is added. Historical records are disabled in the selector, and saved unavailable selections remain visible until explicitly replaced. CI builds have their own artifact identities and do not change product qualification.

The real Server harness covers SQLite with anonymous loopback, static Bearer, HTTPS with explicit CA/base path, and injected-provider/enforced access. It exercises save/search/exact reads, identity changes, binding revocation, ambiguous writes without replay and Scope pagination. Installed UI checks cover repeated launches preserving the existing editor, late committed-save responses preserving a new Scope draft, explicit connection/Scope selection, clipboard copies, connection isolation, draft cancellation, byte limits and independent Server survival after Desktop exit.

Selecting a compatibility profile does not attest the remote binary identity. Existing `sqlite-1.1.1-v1` / `sqlite-63f918b7-v1` / `sqlite-ab43e3a7-v1` / `sqlite-f1089f4e-v1` / `sqlite-6e237568-v1` / `sqlite-legacy-58f7f4f6-v1` selections require explicitly choosing an available matching profile and rechecking the connection. Desktop reads memories only through Atomic Memory references, so Servers that answer only with Memory entry citations are not qualified. CLI diagnostics separately verify the registered executable path and digest, then validate the diagnostic response schema. The observed version is optional report information; a registration does not require a version.

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
pnpm --dir desktop tauri build --bundles nsis --ci --features ci-fixtures -- --locked
```

## Remaining release qualification

- Clean standard-user Windows 11, including absent/present WebView2 and bootstrap recovery.
- Publisher signing and release ownership.
- Actual IME, screen reader, high contrast and complete keyboard accessibility.
- Notification cold activation, independent service login/reboot behavior and a named Agent host's capture/recall workflow.
- Maintainer ownership and approved performance budgets.

Hosted Windows CI and developer-machine checks do not replace these platform and product gates. This preview does not close #1654 or #1428.
