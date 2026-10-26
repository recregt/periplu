{
  description = "periplu";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forEach = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      checks = forEach (pkgs: {
        ruff =
          pkgs.runCommand "periplu-ruff"
            {
              nativeBuildInputs = [ pkgs.ruff ];
              root = pkgs.lib.fileset.toSource {
                root = ./.;
                fileset = pkgs.lib.fileset.unions [
                  ./src
                  ./ruff.toml
                ];
              };
              RUFF_NO_CACHE = "true";
            }
            ''
              cd $root
              ruff check .
              ruff format --check .
              touch $out
            '';
      });

      formatter = forEach (pkgs: pkgs.nixfmt);

      devShells = forEach (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python314
            pkgs.uv
            pkgs.ruff
            pkgs.lefthook
          ];
        };
      });
    };
}
