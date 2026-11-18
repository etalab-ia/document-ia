{
  description = "Document IA — Reproducible Nix environments & verified model artifacts (Issue #166)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          lib = pkgs.lib;

          yoloworldWeights = pkgs.fetchurl {
            name = "yolov8m-world.pt";
            url = "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8m-world.pt";
            sha256 = "8ede3cfa80c243608a9ef14d54f55030efc9d7a8635af245fc6d67efdf294aaa";
          };

          qrdetWeights = pkgs.fetchurl {
            name = "qrdet-s.pt";
            url = "https://github.com/Eric-Canas/qrdet/releases/download/v2.0_release/qrdet-s.pt";
            sha256 = "50a9113970a878a51206d5caabcc2c758856f751944199a2c371d872fd768af1";
          };

          tesseractFraTraineddata = pkgs.fetchurl {
            name = "fra.traineddata";
            url = "https://github.com/tesseract-ocr/tessdata_fast/raw/4.1.0/fra.traineddata";
            sha256 = "ced037562e8c80c13122dece28dd477d399af80911a28791a66a63ac1e3445ca";
          };

          modelArtifacts = pkgs.runCommand "document-ia-model-artifacts" { } ''
            mkdir -p "$out/qrdet" "$out/tessdata"
            cp "${yoloworldWeights}" "$out/yolov8m-world.pt"
            cp "${qrdetWeights}" "$out/qrdet/qrdet-s.pt"
            printf '%s' "https://github.com/Eric-Canas/qrdet/releases/download/v2.0_release" > "$out/qrdet/current_release.txt"
            cp "${tesseractFraTraineddata}" "$out/tessdata/fra.traineddata"
            chmod -R u+rwX,go+rX "$out"
          '';

          commonPackages = [
            pkgs.python314
            pkgs.uv
            pkgs.git
            pkgs.bashInteractive
            pkgs.coreutils
            pkgs.findutils
            pkgs.gnugrep
            pkgs.cacert
            pkgs.tzdata
            pkgs.zlib
            pkgs.stdenv.cc.cc.lib
            pkgs.openssl
          ];

          mkRuntimeEnv = name: extraPackages:
            pkgs.buildEnv {
              inherit name;
              paths = commonPackages ++ extraPackages;
              pathsToLink = [ "/bin" "/lib" "/share" "/etc" ];
              extraOutputsToInstall = [ "out" "lib" ];
            };
        in
        {
          model-artifacts = modelArtifacts;

          runtime-api = mkRuntimeEnv "document-ia-runtime-api" [
            pkgs.file
          ];

          runtime-worker = mkRuntimeEnv "document-ia-runtime-worker" (
            [
              pkgs.file
              pkgs.tesseract
              pkgs.glib
            ]
            ++ lib.optionals pkgs.stdenv.isLinux [
              pkgs.libGL
              pkgs.libxcb
              pkgs.xorg.libX11
              pkgs.xorg.libXext
              pkgs.xorg.libSM
              pkgs.xorg.libICE
            ]
          );

          runtime-scheduler = mkRuntimeEnv "document-ia-runtime-scheduler" [
            pkgs.file
          ];

          runtime-evals = mkRuntimeEnv "document-ia-runtime-evals" [
            pkgs.file
          ];

          runtime-auto-scaler = mkRuntimeEnv "document-ia-runtime-auto-scaler" [ ];
        }
      );

      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python314
              pkgs.uv
              pkgs.git
              pkgs.tesseract
              pkgs.file
            ];
          };
        }
      );
    };
}
