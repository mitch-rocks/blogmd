import sys

try:
    from mdserver.cli import main
    import flask, markdown, frontmatter, nh3  # noqa: F401  (fail fast with a helpful message)
except ModuleNotFoundError as e:
    sys.exit(
        f'Missing package "{e.name}" for the Python running this script:\n'
        f'    {sys.executable}\n\n'
        f'Install the requirements into that same interpreter:\n'
        f'    "{sys.executable}" -m pip install -r requirements.txt\n'
    )

if __name__ == '__main__':
    main()
