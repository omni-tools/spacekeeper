"""Check shipped source, metadata, license, and private-file exclusions."""

from email.parser import BytesParser
from pathlib import Path, PurePosixPath
import tarfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PRIVATE = {'.git', '.vscode', '.venv', '.aws', '.codex', '.agents', 'private',
           'snapshots', 'backups', 'reports', 'presets', '__pycache__'}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def check_names(names: list[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts, f'Unsafe archive path: {name}')
        require(not PRIVATE.intersection(path.parts), f'Private runtime path packaged: {name}')
        require(path.name != '.env' and not path.name.startswith('.env.') and not path.name.endswith('.pyc'),
                f'Local/generated file packaged: {name}')
    require(any(name.endswith('/LICENSE') for name in names), 'MIT license file missing from distribution.')


def main() -> None:
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    wheels, sources = list((ROOT / 'dist').glob('*.whl')), list((ROOT / 'dist').glob('*.tar.gz'))
    require(len(wheels) == len(sources) == 1, 'Expected exactly one wheel and one source distribution in dist/.')
    modules = sorted((ROOT / 'src/spacekeeper').glob('*.py'))
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        check_names(names)
        metadata_files = [name for name in names if name.endswith('.dist-info/METADATA')]
        require(len(metadata_files) == 1, 'Expected one wheel metadata record.')
        metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
        require(metadata['Name'] == project['name'] and metadata['Version'] == project['version'],
                'Wheel name/version differs from pyproject.toml.')
        require(metadata['License-Expression'] == 'MIT', 'Wheel license metadata is missing or incorrect.')
        for source in modules:
            name = 'spacekeeper/' + source.name
            require(name in names and archive.read(name) == source.read_bytes(), f'Wheel source differs: {name}')
    with tarfile.open(sources[0]) as archive:
        names = archive.getnames()
        check_names(names)
        for source in [*modules, *sorted((ROOT / 'tests').glob('*.py')), ROOT / 'README.md',
                       ROOT / 'pyproject.toml', Path(__file__).resolve()]:
            relative = source.relative_to(ROOT).as_posix()
            matches = [name for name in names if name.endswith('/' + relative)]
            require(len(matches) == 1, f'Source distribution omits or duplicates: {relative}')
            stream = archive.extractfile(matches[0])
            require(stream is not None, f'Source archive member is not a file: {relative}')
            packaged = stream.read()
            if source.name == 'pyproject.toml':
                # The backend may normalize TOML syntax when creating an sdist.
                require(tomllib.loads(packaged.decode()) == tomllib.loads(source.read_text()),
                        'Source archive project metadata differs.')
            else:
                require(packaged == source.read_bytes(), f'Source archive differs: {relative}')
    print('Wheel and source distribution verified: current modules/tests, metadata, license, and private-path exclusions.')


if __name__ == '__main__':
    main()
