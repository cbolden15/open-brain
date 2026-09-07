# OB1-W0: base package and automatic bootstrap

Status: complete; `OB1-W1` not started

Date: 2026-09-07

Starting commit: `10d0846b56144ac8ad316452bd31cf6756844d05`

Branch: `goal/open-brain-five-minute-install`

Documentation gate: independent read-only review returned `READY` on 2026-09-07 with zero P0, P1,
or P2 findings.

## Objective

Make the default `open-brain` installation a small, daemonless local product that can create one
private Brain automatically. Preserve the existing appliance and Secure Node code behind an
explicit opt-in profile. This workstream prepares the five-minute journey without implementing its
capture, search, or export commands.

## Decisions

1. The default distribution remains `open-brain`. Its unconditional runtime dependency is exactly
   `open-brain-engine==0.1.0`; it inherits only that engine's base cross-product dependencies. The
   application metadata declares the following selected graph:

   ```toml
   dependencies = ["open-brain-engine==0.1.0"]

   [project.optional-dependencies]
   secure-node = [
     "open-brain-engine[secure-node]==0.1.0",
     "starlette>=0.48,<1",
     "uvicorn>=0.40,<1",
   ]
   ```

   The engine's renamed `secure-node` extra owns Argon2, cryptography, keyring, SQLCipher, and the
   macOS user-presence bridge. No other app extra selects it.
2. The installed command contract is frozen as follows:

   | Command | Target | Base-only behavior |
   |---|---|---|
   | `open-brain` | local default CLI | Available; imports no Secure Node module |
   | `python -m open_brain` | local default CLI | Same behavior as `open-brain` |
   | `open-brain-secure-node` | lazy Secure Node CLI wrapper | Exits `2` with `Secure Node is not installed. Install open-brain[secure-node].` before state access |
   | `open-brain-secure-node-mcp` | lazy Secure Node MCP wrapper | Same unavailable result before state access |

   Python extras cannot conditionally publish console scripts, so the two Secure Node launchers are
   harmless standard-library-only wrappers in a base installation. They may import the retained
   appliance entry points only after the optional dependency set is present. The ambiguous
   pre-split `open-brain-mcp` script is removed from package metadata; its implementation remains in
   source as Secure Node code.
3. The path vocabulary is exact. The application data home is
   `$HOME/Library/Application Support/open-brain` on macOS and
   `${XDG_DATA_HOME:-$HOME/.local/share}/open-brain` on Linux. The Brain root is always the `brain`
   child of that default data home, matching the product contract. Executable payloads live at
   `$HOME/.local/lib/open-brain`, and the launcher lives at `$HOME/.local/bin/open-brain`; neither
   is stored under the application data home.
4. An explicit absolute `--data-dir` names the Brain root itself, not its parent application data
   home. It is an expert/test override, never a prompt or required input. The local default CLI does
   not consume the retained Secure Node and legacy-test `OPEN_BRAIN_ROOT` variable.
5. The base installer places the launcher in `$HOME/.local/bin`. The acceptance shell prepends that
   conventional directory after installation because a piped child process cannot modify its
   parent shell's environment.
6. The existing one-folder appliance artifact remains Secure Node precursor evidence. Open Brain
   gets a separate minimal artifact path rather than weakening or relabeling that artifact.

If dependency or native-artifact inspection shows that the optional extra cannot isolate Secure
Node code and dependencies, stop and use the already accepted separate
`open-brain-secure-node` distribution fallback.

## Work packages

### 1. Package and command isolation

- [x] Make base `open-brain` depend on base `open-brain-engine` only.
- [x] Rename the engine `node` extra to `secure-node` and put Starlette and Uvicorn in the app's
      matching extra.
- [x] Route `open-brain` and `python -m open_brain` to a local default entry point.
- [x] Route the two frozen Secure Node commands through standard-library-only wrappers that fail
      with the documented exit code and message when the extra is absent, then lazy-import the
      retained appliance CLI and MCP entry points when it is installed.
- [x] Build wheels and test them in two fresh environments. The base environment must prove its
      resolved dependency graph, script behavior, and imported-module inventory. The extra
      environment must prove the same wheel exposes working Secure Node help/MCP dispatch and the
      complete renamed dependency closure.
- [x] If those installed-wheel tests cannot enforce this boundary, stop and implement the accepted
      separate-distribution fallback instead of weakening the base contract.

### 2. Platform-local root policy

- [x] Resolve the documented application data home and its `brain` child without prompts or
      configuration. Assert the exact macOS and Linux defaults and prove an absolute `--data-dir`
      selects the Brain root directly.
- [x] Walk from a trusted absolute owner-controlled anchor with directory descriptors and
      no-follow operations. Validate every existing component below that anchor before creation:
      each must be a directory, must not be a symlink, and must have `st_uid == geteuid()`. Never use
      `Path.resolve()` or recursive `mkdir()` as the security decision.
- [x] Define that anchor deterministically. Walk the absolute target from `/` with no-follow
      descriptors; root-owned system prefixes are traversal-only, and the deepest existing prefix
      at which creation begins must be owned by the effective user. From the first user-owned
      component onward, every existing descendant must have the same owner. Reject a target with no
      user-owned creation anchor. Tests may inject an owner-controlled temporary anchor without
      weakening production traversal.
- [x] Treat an existing application data home or Brain root with group/world permission bits as an
      error. Do not repair it with `chmod`. Create missing managed directories descriptor-relative
      with mode `0700` under `umask 077`, and create sensitive regular files with mode `0600`.
- [x] Reject these synchronized locations after component-aware normalization: on macOS,
      `~/Library/Mobile Documents`, `~/Library/CloudStorage`, Dropbox, OneDrive, and Google Drive;
      on Linux, Dropbox, OneDrive, Google Drive, Nextcloud, Syncthing, and `~/Sync`.
- [x] Reject these mounted filesystem types: on macOS, `afpfs`, `nfs`, `smbfs`, and `webdav`; on
      Linux, `9p`, `afs`, `ceph`, `cifs`, `davfs`, `fuse.sshfs`, `nfs`, `nfs4`, and `smb3`. Determine
      the type with Darwin `statfs` or Linux `/proc/self/mountinfo`; fail closed when the supported
      host cannot classify the selected root.
- [x] Keep the validated root descriptor and its `(st_dev, st_ino, filesystem_type)` evidence
      through bootstrap. Re-open from the trusted anchor and compare that identity immediately
      before the first owner/Brain identity write and again immediately before SQLite creates or
      opens its file. Abort before that write if any path component or identity changed.
- [x] Keep the existing identity stable on repeated bootstrap.

### 3. Daemonless bootstrap

- [x] Add `open-brain init` as an idempotent local bootstrap command.
- [x] Create one owner, one Brain, and the SQLite schema through the existing direct local engine.
- [x] Start no listener, daemon, supervisor, container, connector, grant, or key-custody path.
- [x] Report the profile as local SQLite with daemon and application encryption both false.

### 4. Installer and artifact boundary

- [x] Add a POSIX installer for the supported macOS-arm64 and Linux-x86_64 release assets.
- [x] Verify the release manifest and selected artifact checksum before activation.
- [x] Install without sudo and keep executable files outside the Open Brain data directory.
- [x] Add a minimal base-native entry point/spec whose collected modules exclude Secure Node,
      connector, legacy, server, crypto, and service-management code.
- [x] Keep release publication and clean-host matrix execution in `OB1-W2`.

### 5. Verification and closure

- [x] Add focused package, import-isolation, data-root, permission, bootstrap, installer, and
      artifact-membership tests. Root tests cover ancestor symlinks, foreign ownership, permissive
      existing managed roots, every named synchronized/network policy, and replacement races at
      both pre-write revalidation points.
- [x] Update package classification, artifact policy, generated reports, README, and install docs.
- [x] Run focused tests, Ruff, strict MyPy, and shellcheck; build and inspect the base Python
      artifacts and the current host's native artifact; and validate the Linux native CI and policy
      contracts without claiming clean-host execution.
- [x] Run `make verify` on the exact committed tree and obtain independent read-only review with no
      unresolved P0 through P2 findings.

## Closure evidence

The implementation is recorded in commits `0abca70` and `ebef782`. The second commit closes all
findings from the first implementation review.

| Gate | Result on exact implementation commit `ebef782afa066fb5108342f529cd0635b804eacd` |
|---|---|
| Repository verification | `make verify` passed Ruff, strict MyPy over 593 source files, 3,510 tests, all six Python artifact builds, and artifact-policy verification. |
| Focused W0 verification | `make ob1w0-preflight` passed 191 tests, shellcheck, move-manifest validation, focused Ruff and strict MyPy, and diff integrity. The tests and `actionlint .github/workflows/ci.yml` validate the unpublished Linux-x86_64 native build and policy contracts without treating them as host-execution evidence. `uv lock --check` also passed. |
| Native artifact | `make ob1w0-native` built and exercised the Python 3.14 macOS-arm64 archive. Its 208-module inventory digest is `1c0ea3c94466f091d9e80b3ceecc965f8a9eb91a758b999b65b5e5205ca3c4a9`; its tree digest is `e7c64e5fadf6620ae198e4e7718ad4f3e403d70ccacc28668fdf5630f9a59d52`; and its checksum-verified archive digest is `46bbb25bb271d1c6188f403833c93ce8123a08fa0c6765038ea472962876883a`. The build includes the state-free self-check, both documented `init` option orders, idempotent bootstrap, and installer activation smoke. |
| Independent implementation review | Read-only review returned `READY` with P0/P1/P2 at 0/0/0. It confirmed that `OB1-W1` commands and `OB1-W2` publication or clean-host execution are absent. |

No artifact was published or pushed. Linux native execution, release assembly, reinstall proof, and
the exact clean-host 300-second matrix remain in `OB1-W2`.

## Exit criteria

`OB1-W0` closes when fresh base and Secure Node-extra environments prove the frozen metadata and
command graph, a clean base environment can install or execute the base artifact, help and version
load no Secure Node module, and bootstrap creates exactly the documented `brain` root. Repeated
bootstrap must reopen the same owner and Brain, show a SQLite store with no background runtime, and
leave unsafe or replaced roots untouched. The default resolved dependency graph and native
artifact must contain no Secure Node-only dependency.

Stop there. `OB1-W1` owns direct capture, search, export, status, and doctor behavior. `OB1-W2`
owns published artifacts and the exact 300-second host matrix. `CORE-W0` remains paused, and
`SN1-W2` remains gated.
