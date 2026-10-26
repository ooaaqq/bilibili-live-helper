{
  craneLib,
  lib,
  linkFarm,
  pkg-config,
  sqlite,
  makeWrapper,
  cacert,
  git,
  cmake,
  perl,
  rustPlatform,
}:
let
  productionSource = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./Cargo.toml
      ./Cargo.lock
      (lib.fileset.fileFilter (file: file.hasExt "rs" && !(lib.hasSuffix "_tests.rs" file.name)) ./src)
    ];
  };
  checkSource = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./Cargo.toml
      ./Cargo.lock
      ./src
      ./tests
      ./config.example.yaml
    ];
  };
  common = {
    pname = "bilibili-live-helper";
    version = (builtins.fromTOML (builtins.readFile ./Cargo.toml)).package.version;
    src = productionSource;
    strictDeps = true;
    nativeBuildInputs = [
      pkg-config
      git
      cmake
      perl
      rustPlatform.bindgenHook
    ];
    buildInputs = [ sqlite ];
    SSL_CERT_FILE = "${cacert}/etc/ssl/certs/ca-bundle.crt";
  };
  cargoArtifacts = craneLib.buildDepsOnly common;
  application = craneLib.buildPackage (
    common
    // {
      inherit cargoArtifacts;
      doCheck = false;
      nativeBuildInputs = common.nativeBuildInputs ++ [ makeWrapper ];
      postInstall = ''
        wrapProgram "$out/bin/bilibili-live-helper" --set SSL_CERT_FILE ${cacert}/etc/ssl/certs/ca-bundle.crt
      '';
      passthru.checks = linkFarm "bilibili-live-helper-checks" [
        {
          name = "tests";
          path = tests;
        }
        {
          name = "clippy";
          path = clippy;
        }
        {
          name = "format";
          path = format;
        }
      ];
      meta.mainProgram = "bilibili-live-helper";
    }
  );
  checkArgs = common // {
    inherit cargoArtifacts;
    src = checkSource;
  };
  tests = craneLib.cargoTest (
    checkArgs
    // {
    }
  );
  clippy = craneLib.cargoClippy (
    checkArgs // { cargoClippyExtraArgs = "--all-targets -- -D warnings"; }
  );
  format = craneLib.cargoFmt { src = checkSource; };
in
application
