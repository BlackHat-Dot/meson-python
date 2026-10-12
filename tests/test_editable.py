# SPDX-FileCopyrightText: 2021 The meson-python developers
#
# SPDX-License-Identifier: MIT

import importlib
import io
import os
import pathlib
import pkgutil
import re
import subprocess
import sys

from contextlib import redirect_stdout

import pytest
import wheel.wheelfile

import mesonpy

from mesonpy import _editable

from .conftest import EXT_SUFFIX, FREE_THREADED_BUILD


def find_cython_version():
    cython_version_str = subprocess.run(['cython', '--version'], check=True,
                                        stdout=subprocess.PIPE, text=True).stdout
    version_str = re.search(r'(\d{1,4}\.\d{1,4}\.?\d{0,4})', cython_version_str).group(0)
    return tuple(map(int, version_str.split('.')))

CYTHON_VERSION = find_cython_version()


def test_walk(package_complex):
    entries = _editable.walk(
        os.fspath(package_complex / 'complex'),
        [os.path.normpath('more/meson.build'), os.path.normpath('more/baz.pyx')],
        [os.path.normpath('namespace')],
    )
    assert {pathlib.Path(x) for x in entries} == {
        pathlib.Path('__init__.py'),
        pathlib.Path('more/__init__.py'),
    }


def test_nodes_tree():
    tree = _editable.Node()
    tree[('aa', 'bb', 'cc')] = 'path1'
    tree[('aa', 'dd')] = 'path2'
    assert tree['aa']['bb']['cc'] == 'path1'
    assert tree['aa']['dd'] == 'path2'
    assert tree[('aa', 'bb', 'cc')] == 'path1'
    assert tree.get(('aa', 'bb', 'cc')) == 'path1'
    assert tree['aa'] == tree[('aa', )]
    assert tree.get(('aa', 'gg')) is None
    assert tree[('aa', 'gg')] == _editable.Node()


def test_collect(package_complex):
    root = os.fspath(package_complex)
    install_plan = {
        'targets': {
            os.path.join(root, 'build', f'test{EXT_SUFFIX}'): {
                'destination': os.path.join('{py_platlib}', 'complex', f'test{EXT_SUFFIX}'),
                'tag': 'runtime'},
        },
        'install_subdirs': {
            os.path.join(root, 'complex'): {
                'destination': os.path.join('{py_platlib}', 'complex'),
                'tag': None}
        }
    }
    tree = _editable.collect(install_plan)
    assert tree['complex']['__init__.py'] == os.path.join(root, 'complex', '__init__.py')
    assert tree['complex'][f'test{EXT_SUFFIX}'] == os.path.join(root, 'build', f'test{EXT_SUFFIX}')
    assert tree['complex']['more']['__init__.py'] == os.path.join(root, 'complex', 'more', '__init__.py')


@pytest.mark.skipif(FREE_THREADED_BUILD and CYTHON_VERSION < (3, 1, 0),
                    reason='Cython version too old, no free-threaded CPython support')
def test_mesonpy_meta_finder(package_complex, tmp_path):
    # build a package in a temporary directory
    project = mesonpy.Project(package_complex, tmp_path)

    # point the meta finder to the build directory
    finder = _editable.MesonpyMetaFinder('complex', {'complex'}, os.fspath(tmp_path), project._build_command, True)

    # check repr
    assert repr(finder) == f'MesonpyMetaFinder(\'complex\', {str(tmp_path)!r})'

    # verify that we can look up a pure module in the source directory
    spec = finder.find_spec('complex')
    assert spec.name == 'complex'
    assert isinstance(spec.loader, _editable.SourceFileLoader)
    assert spec.origin == os.fspath(package_complex / 'complex/__init__.py')

    # and an extension module in the build directory
    spec = finder.find_spec('complex.test')
    assert spec.name == 'complex.test'
    assert isinstance(spec.loader, _editable.ExtensionFileLoader)
    assert spec.origin == os.fspath(tmp_path / f'test{EXT_SUFFIX}')

    try:
        # install the finder in the meta path
        sys.meta_path.insert(0, finder)
        # verify that we can import the modules
        import complex
        assert complex.__spec__.origin == os.fspath(package_complex / 'complex/__init__.py')
        assert complex.__file__ == os.fspath(package_complex / 'complex/__init__.py')
        import complex.extension
        assert complex.extension.__spec__.origin == os.fspath(tmp_path / f'extension{EXT_SUFFIX}')
        assert complex.extension.answer() == 42
        import complex.namespace.foo
        assert complex.namespace.foo.__spec__.origin == os.fspath(package_complex / 'complex/namespace/foo.py')
        assert complex.namespace.foo.foo() == 'foo'
    finally:
        # remove finder from the meta path
        del sys.meta_path[0]


def test_mesonpy_traversable():
    tree = _editable.Node()
    tree[('package', '__init__.py')] = '/tmp/src/package/__init__.py'
    tree[('package', 'src.py')] = '/tmp/src/package/src.py'
    tree[('package', 'data.txt')] = '/tmp/src/package/data.txt'
    tree[('package', 'nested', '__init__.py')] = '/tmp/src/package/nested/__init__.py'
    tree[('package', 'nested', 'some.py')] = '/tmp/src/package/nested/some.py'
    tree[('package', 'nested', 'generated.txt')] = '/tmp/build/generated.txt'
    traversable = _editable.MesonpyTraversable('package', tree['package'])
    assert {x.name for x in traversable.iterdir()} == {'__init__.py', 'src.py', 'data.txt', 'nested'}
    nested = traversable / 'nested'
    assert nested.is_dir()
    assert {x.name for x in nested.iterdir()} == {'__init__.py', 'some.py', 'generated.txt'}
    generated = traversable.joinpath('nested', 'generated.txt')
    assert isinstance(generated, pathlib.Path)
    assert generated == pathlib.Path('/tmp/build/generated.txt')
    bad = traversable / 'bad'
    assert not bad.is_file()
    assert not bad.is_dir()
    with pytest.raises(FileNotFoundError):
        bad.open()


def test_resources(tmp_path):
    # build a package in a temporary directory
    package_path = pathlib.Path(__file__).parent / 'packages' / 'simple'
    project = mesonpy.Project(package_path, tmp_path)

    # point the meta finder to the build directory
    finder = _editable.MesonpyMetaFinder('simple', {'simple'}, os.fspath(tmp_path), project._build_command, True)

    # verify that we can look up resources
    spec = finder.find_spec('simple')
    reader = spec.loader.get_resource_reader('simple')
    traversable = reader.files()
    assert {x.name for x in traversable.iterdir()} == {'__init__.py', 'test.py', 'data.txt'}
    with traversable.joinpath('data.txt').open() as f:
        text = f.read().rstrip()
    assert text == 'ABC'


def test_importlib_resources(tmp_path):
    # build a package in a temporary directory
    package_path = pathlib.Path(__file__).parent / 'packages' / 'simple'
    project = mesonpy.Project(package_path, tmp_path)

    # point the meta finder to the build directory
    finder = _editable.MesonpyMetaFinder('simple', {'simple'}, os.fspath(tmp_path), project._build_command, True)

    try:
        # install the finder in the meta path
        sys.meta_path.insert(0, finder)
        # verify that we can import the modules
        import simple
        assert simple.__spec__.origin == os.fspath(package_path / '__init__.py')
        assert simple.__file__ == os.fspath(package_path / '__init__.py')
        assert simple.data() == 'ABC'
        # load resources via importlib
        import importlib.resources
        with importlib.resources.files(simple).joinpath('data.txt').open() as f:
            text = f.read().rstrip()
        assert text == 'ABC'
        assert importlib.resources.files(simple).joinpath('data.txt').read_text().rstrip() == 'ABC'
    finally:
        # remove finder from the meta path
        del sys.meta_path[0]


def test_editable_install(venv, editable_simple):
    venv.pip('install', os.fspath(editable_simple))
    assert venv.python('-c', 'import simple; print(simple.data())').strip() == 'ABC'


def test_editable_contents(editable_simple):
    artifact = wheel.wheelfile.WheelFile(editable_simple)

    impl = 'start' if sys.version_info >= (3, 15) else 'pth'
    expecting = {
        'simple-1.0.0.dist-info/METADATA',
        'simple-1.0.0.dist-info/RECORD',
        'simple-1.0.0.dist-info/WHEEL',
        '_simple_editable_loader.py',
        f'simple-editable.{impl}',
    }

    assert set(artifact.namelist()) == expecting


def test_editble_reentrant(venv, editable_imports_itself_during_build):
    venv.pip('install', os.fspath(editable_imports_itself_during_build))
    assert venv.python('-c', 'import plat; print(plat.data())').strip() == 'ABC'

    path = pathlib.Path(__file__).parent / 'packages' / 'imports-itself-during-build' / 'plat.c'
    code = path.read_text()

    try:
        # edit souce code
        path.write_text(code.replace('ABC', 'DEF'))
        # check that the module is rebuilt on import. the build proess
        # imports python code from the source directory, thus this
        # test also check that this import does not cause an rebuild
        # loop
        assert venv.python('-c', 'import plat; print(plat.data())').strip() == 'DEF'
    finally:
        path.write_text(code)


@pytest.mark.skipif(FREE_THREADED_BUILD and CYTHON_VERSION < (3, 1, 0),
                    reason='Cython version too old, no free-threaded CPython support')
def test_editable_pkgutils_walk_packages(package_complex, tmp_path):
    # build a package in a temporary directory
    project = mesonpy.Project(package_complex, tmp_path)

    finder = _editable.MesonpyMetaFinder('complex', {'complex'}, os.fspath(tmp_path), project._build_command, True)

    try:
        # install editable hooks
        sys.meta_path.insert(0, finder)
        sys.path_hooks.insert(0, finder._path_hook)

        import complex
        packages = {m.name for m in pkgutil.walk_packages(complex.__path__, complex.__name__ + '.')}
        assert packages == {
            'complex.bar',
            'complex.extension',
            'complex.more',
            'complex.more.baz',
            'complex.more.move',
            'complex.test',
        }

        from complex import namespace
        packages = {m.name for m in pkgutil.walk_packages(namespace.__path__, namespace.__name__ + '.')}
        assert packages == {
            'complex.namespace.bar',
            'complex.namespace.foo',
        }

    finally:
        # remove hooks
        del sys.meta_path[0]
        del sys.path_hooks[0]


def test_custom_target_install_dir(package_custom_target_dir, tmp_path):
    project = mesonpy.Project(package_custom_target_dir, tmp_path)
    finder = _editable.MesonpyMetaFinder('package', {'package'}, os.fspath(tmp_path), project._build_command, True)
    try:
        sys.meta_path.insert(0, finder)
        import package.generated.one
        import package.generated.two  # noqa: F401
    finally:
        del sys.meta_path[0]


@pytest.mark.parametrize('verbose', [False, True], ids=('', 'verbose'))
@pytest.mark.parametrize('args', [[], ['-j1']], ids=('', '-Ccompile-args=-j1'))
def test_editable_rebuild(package_purelib_and_platlib, tmp_path, verbose, args):
    with mesonpy._project({'builddir': os.fspath(tmp_path), 'compile-args': args}) as project:

        finder = _editable.MesonpyMetaFinder(
            project._metadata.name, {'plat', 'pure'},
            os.fspath(tmp_path), project._build_command,
            verbose=verbose,
        )

        try:
            # Install editable hooks
            sys.meta_path.insert(0, finder)

            # Import module and trigger rebuild. Importing any module in the
            # Python package triggers the build. Use the the pure Python one as
            # Cygwin is not happy when reloading an extension module.
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                import pure
            assert not verbose or stdout.getvalue().startswith('meson-python: building ')

            # Reset state.
            del sys.modules['pure']
            finder._rebuild.cache_clear()

            # Importing again should result in no output.
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                import pure  # noqa: F401
            assert stdout.getvalue() == ''

        finally:
            del sys.meta_path[0]
            sys.modules.pop('pure', None)


@pytest.mark.skipif(FREE_THREADED_BUILD and CYTHON_VERSION < (3, 1, 0),
                    reason='Cython version too old, no free-threaded CPython support')
def test_editable_verbose(venv, package_complex, editable_complex, monkeypatch):
    monkeypatch.setenv('MESONPY_EDITABLE_VERBOSE', '1')
    venv.pip('install', os.fspath(editable_complex))

    # Importing the module should not result in any output since the project has already been built
    assert venv.python('-c', 'import complex').strip() == ''

    # Touch a compiled source file and make sure that the build info is output on import
    package_complex.joinpath('test.pyx').touch()
    output = venv.python('-c', 'import complex').strip()
    assert output.startswith('meson-python: building complex: ')

    # Another import without file changes should not show any output
    assert venv.python('-c', 'import complex') == ''


@pytest.mark.parametrize('verbose', [False, True], ids=('', 'verbose'))
def test_editable_rebuild_error(package_purelib_and_platlib, tmp_path, verbose):
    with mesonpy._project({'builddir': os.fspath(tmp_path)}) as project:

        finder = _editable.MesonpyMetaFinder(
            project._metadata.name, {'plat', 'pure'},
            os.fspath(tmp_path), project._build_command,
            verbose=verbose,
        )
        path = package_purelib_and_platlib / 'plat.c'
        code = path.read_text()

        try:
            # Install editable hooks
            sys.meta_path.insert(0, finder)

            # Insert invalid code in the extension module source code
            path.write_text('return')

            # Import module and trigger rebuild: the build fails and ImportError is raised
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                with pytest.raises(ImportError, match='rebuilding the "purelib-and-platlib" ') as exc:
                    import plat  # noqa: F401
            assert not verbose or stdout.getvalue().startswith('meson-python: building ')

            if sys.version_info >= (3, 11):
                assert verbose or hasattr(exc.value, '__notes__')
                assert verbose or 'ninja: build stopped: subcommand failed.' in exc.value.__notes__[0]
            else:
                assert verbose or 'ninja: build stopped: subcommand failed.' in exc.value.msg

        finally:
            del sys.meta_path[0]
            sys.modules.pop('pure', None)
            path.write_text(code)


def test_editable_reconfigure_error(package_purelib_and_platlib, tmp_path):
    with mesonpy._project({'builddir': os.fspath(tmp_path)}) as project:

        finder = _editable.MesonpyMetaFinder(
            project._metadata.name, {'plat', 'pure'},
            os.fspath(tmp_path), project._build_command,
            verbose=False,
        )
        path = package_purelib_and_platlib / 'meson.build'
        code = path.read_text()

        try:
            # Install editable hooks
            sys.meta_path.insert(0, finder)

            # Emit an error (with unicode characters) during reconfigure
            with open(path, 'a', encoding='utf8') as f:
                f.write('\n\nerror(\'injected error \N{BOMB}\')\n')

            # Import module and trigger rebuild: the build fails and ImportErrror is raised
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                with pytest.raises(ImportError, match='rebuilding the "purelib-and-platlib" ') as exc:
                    import plat  # noqa: F401
            msg = exc.value.__notes__[0] if sys.version_info >= (3, 11) else exc.value.msg
            assert 'ERROR: Problem encountered: injected error' in msg
            assert 'ninja: error: rebuilding \'build.ninja\': subcommand failed' in msg

        finally:
            del sys.meta_path[0]
            sys.modules.pop('pure', None)
            path.write_text(code)


def test_install_data(venv, editable_install_data, tmp_path):
    venv.pip('install', os.fspath(editable_install_data))
    venv.python('-c', 'import package')


@pytest.mark.parametrize('first,second', [('main', 'dep'), ('dep', 'main')])
def test_editable_namespace_package(tmp_path, monkeypatch, first, second):
    site_packages = tmp_path / 'site-packages'
    dep_dir = site_packages / 'test_ns' / 'dep'
    dep_dir.mkdir(parents=True)
    (dep_dir / '__init__.py').write_text('VALUE = "dep"')

    src_dir = tmp_path / 'src' / 'test_ns' / 'main'
    src_dir.mkdir(parents=True)
    (src_dir / '__init__.py').write_text('VALUE = "main"')

    tree = _editable.Node()
    tree[('test_ns', 'main', '__init__.py')] = os.fspath(src_dir / '__init__.py')

    finder = _editable.MesonpyMetaFinder('main', {'test_ns'}, os.fspath(tmp_path), ['ninja'])
    monkeypatch.setattr(finder, '_rebuild', lambda: tree)

    monkeypatch.syspath_prepend(os.fspath(site_packages))
    monkeypatch.setattr(sys, 'meta_path', [finder, *sys.meta_path])
    monkeypatch.setattr(sys, 'path_hooks', [finder._path_hook, *sys.path_hooks])

    for mod in ['test_ns', 'test_ns.main', 'test_ns.dep']:
        sys.modules.pop(mod, None)

    mod1 = importlib.import_module(f'test_ns.{first}')
    mod2 = importlib.import_module(f'test_ns.{second}')

    assert mod1.VALUE == first
    assert mod2.VALUE == second

    ns = sys.modules['test_ns']
    assert os.fspath(site_packages / 'test_ns') in list(ns.__path__)


def test_editable_multiple_namespace_packages(tmp_path, monkeypatch):
    site_packages = tmp_path / 'site-packages'
    dep_dir = site_packages / 'shared_ns' / 'dep'
    dep_dir.mkdir(parents=True)
    (dep_dir / '__init__.py').write_text('VALUE = "dep"')

    src1 = tmp_path / 'src1' / 'shared_ns' / 'pkg1'
    src1.mkdir(parents=True)
    (src1 / '__init__.py').write_text('VALUE = "pkg1"')
    tree1 = _editable.Node()
    tree1[('shared_ns', 'pkg1', '__init__.py')] = os.fspath(src1 / '__init__.py')

    src2 = tmp_path / 'src2' / 'shared_ns' / 'pkg2'
    src2.mkdir(parents=True)
    (src2 / '__init__.py').write_text('VALUE = "pkg2"')
    tree2 = _editable.Node()
    tree2[('shared_ns', 'pkg2', '__init__.py')] = os.fspath(src2 / '__init__.py')

    finder1 = _editable.MesonpyMetaFinder('pkg1', {'shared_ns'}, os.fspath(tmp_path / 'b1'), ['ninja'])
    monkeypatch.setattr(finder1, '_rebuild', lambda: tree1)

    finder2 = _editable.MesonpyMetaFinder('pkg2', {'shared_ns'}, os.fspath(tmp_path / 'b2'), ['ninja'])
    monkeypatch.setattr(finder2, '_rebuild', lambda: tree2)

    monkeypatch.syspath_prepend(os.fspath(site_packages))
    monkeypatch.setattr(sys, 'meta_path', [finder2, finder1, *sys.meta_path])
    monkeypatch.setattr(sys, 'path_hooks', [finder2._path_hook, finder1._path_hook, *sys.path_hooks])

    for mod in ['shared_ns', 'shared_ns.dep', 'shared_ns.pkg1', 'shared_ns.pkg2']:
        sys.modules.pop(mod, None)

    mod_dep = importlib.import_module('shared_ns.dep')
    mod_pkg1 = importlib.import_module('shared_ns.pkg1')
    mod_pkg2 = importlib.import_module('shared_ns.pkg2')

    assert mod_dep.VALUE == 'dep'
    assert mod_pkg1.VALUE == 'pkg1'
    assert mod_pkg2.VALUE == 'pkg2'
