import ontological


def test_package_imports_with_a_version() -> None:
    assert ontological.__version__


# CI path-filter check; throwaway.
