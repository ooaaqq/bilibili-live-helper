{
  description = "Bilibili Live Helper native Rust application";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs =
    { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      package = pkgs.rustPlatform.buildRustPackage {
        pname = "bilibili-live-helper";
        version = (builtins.fromTOML (builtins.readFile ./Cargo.toml)).package.version;
        src = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [
            ./Cargo.toml
            ./Cargo.lock
            ./src
            ./tests/rust.rs
            ./config.example.yaml
          ];
        };
        cargoHash = "sha256-JVpc1bDfLoFviVwyN8u1XfIVKvy2rDk9XCg24C6eG7g=";
        nativeBuildInputs = [
          pkgs.git
          pkgs.cmake
          pkgs.perl
          pkgs.rustPlatform.bindgenHook
          pkgs.pkg-config
          pkgs.makeWrapper
        ];
        buildInputs = [ pkgs.sqlite ];
        nativeCheckInputs = [ pkgs.clippy ];
        preCheck = "export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
        postCheck = "cargo clippy --release --offline --locked --all-targets -- -D warnings";
        postInstall = ''wrapProgram "$out/bin/bilibili-live-helper" --set SSL_CERT_FILE ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt'';
        meta.mainProgram = "bilibili-live-helper";
      };
    in
    {
      packages.${system}.default = package;
      checks.${system}.application = package;
      formatter.${system} = pkgs.nixfmt-tree;
    };
}
