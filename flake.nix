# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

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
                  ./tests
                  ./examples
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

        tests =
          pkgs.runCommand "periplu-tests"
            {
              nativeBuildInputs = [ (pkgs.python314.withPackages (ps: [ ps.pytest ])) ];
              root = pkgs.lib.fileset.toSource {
                root = ./.;
                fileset = pkgs.lib.fileset.unions [
                  ./src
                  ./tests
                  ./pyproject.toml
                ];
              };
            }
            ''
              cd $root
              PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
              touch $out
            '';

        license = pkgs.runCommand "periplu-license" { nativeBuildInputs = [ pkgs.addlicense ]; } ''
          cd ${./.}
          addlicense -check \
            -ignore 'LICENSE' -ignore '**/uv.lock' -ignore 'flake.lock' -ignore '.python-version' \
            -ignore '**/.venv/**' -ignore '.git/**' .
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
            pkgs.addlicense
            pkgs.lefthook
          ];
          PYTHONDONTWRITEBYTECODE = "1";
          RUFF_NO_CACHE = "true";
          PYTEST_ADDOPTS = "-p no:cacheprovider";
        };
      });
    };
}
