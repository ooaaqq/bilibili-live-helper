{
  description = "Bilibili Live Helper native Nix package";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };
  outputs =
    {
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      inherit (nixpkgs) lib;
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      pythonSet =
        (pkgs.callPackage pyproject-nix.build.packages {
          python = pkgs.python314;
        }).overrideScope
          (
            lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
            ]
          );
      source = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./bilibili_live_helper
          ./tests
          ./pyproject.toml
          ./config.example.yaml
        ];
      };
    in
    {
      packages.${system}.default = pythonSet.mkVirtualEnv "bilibili-live-helper" workspace.deps.default;
      checks.${system} = {
        pytest =
          pkgs.runCommand "bilibili-live-helper-tests"
            {
              src = source;
              nativeBuildInputs = [ (pythonSet.mkVirtualEnv "bilibili-live-helper-tests" workspace.deps.all) ];
            }
            ''
              cp -r "$src" source
              chmod -R u+w source
              cd source
              pytest -p no:cacheprovider
              touch "$out"
            '';
        lint =
          pkgs.runCommand "bilibili-live-helper-lint"
            {
              src = source;
              nativeBuildInputs = [ pkgs.ruff ];
            }
            ''
              cp -r "$src" source
              chmod -R u+w source
              cd source
              ruff check . --no-cache
              ruff format --check . --no-cache
              touch "$out"
            '';
      };
      formatter.${system} = pkgs.nixfmt-tree;
    };
}
