==========================================================================================
iati-docs-management: Docs and tools for developing & maintaining IATI documentation sites
==========================================================================================

This repo serves two purposes:

1. It hosts a Sphinx site (under ``docs/``) with guidance for maintaining IATI documentation sites.
2. It provides tooling (under ``scripts/``) for managing the fleet of IATI documentation repositories as a group.

See also `iati-docs-base <https://github.com/IATI/iati-docs-base>`_ - the template repo that all IATI docs sites are derived from. ``iati-docs-base`` is treated as the authoritative source of truth by the tooling in this repo.

Repository layout
=================

* ``docs/`` - the Sphinx documentation site (maintainer guidance for IATI docs sites).
* ``scripts/`` - reusable tooling for working across all IATI documentation repos.
* ``example-scripts/`` - one-off scripts kept for reference after use.

Many of the scripts rely on the `gh <https://cli.github.com/>`_ command-line tool; ensure it is installed and authenticated against the ``IATI`` org before running them.

Managing the documentation fleet
================================

IATI documentation repos are identified by a GitHub `custom repository property <https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/managing-custom-properties-for-repositories-in-your-organization>`_ named ``Documentation`` set to ``true``. The tooling in ``scripts/`` uses this property to discover the fleet, so any new docs site must be tagged before it will be picked up.

scripts/repo_manager.py
-----------------------

A Python 3.13 CLI for performing checks and updates across every Documentation-tagged repo, using ``iati-docs-base`` as the template. All mutating commands run in **dry-run mode by default** - pass ``--apply`` to actually make changes.

.. code-block:: bash

  # List every Documentation-tagged repo
  python scripts/repo_manager.py list

  # Check each repo's shared files against the template, with diffs
  python scripts/repo_manager.py check

  # Show what would be synced from the template (dry run)
  python scripts/repo_manager.py sync

  # Actually copy template files into each checkout
  python scripts/repo_manager.py sync --apply

  # Commit and push any local changes across the fleet
  python scripts/repo_manager.py push -m "Sync from template" --apply

  # Run an arbitrary script in each repo (repo name is passed as argv[1])
  python scripts/repo_manager.py run-script ./my-script.sh --apply

By default, ``check`` and ``sync`` operate on:

* ``.readthedocs.yaml``
* ``requirements.txt``
* ``.github/workflows/ci.yml``
* ``.vscode/launch.json``

Use ``--files`` on either command to override that list.

Each invocation clones every tagged repo (plus the template) into a temporary directory that is cleaned up when the command exits, so there is no persistent local state to manage.

For more complex checks or syncs, ``RepoManager`` exposes ``run_custom_check`` and ``run_custom_sync`` which accept a callable to run against each checkout. See ``example_check_python_version`` and ``example_sync_gitignore`` in ``scripts/repo_manager.py`` for the expected shape.

Other scripts
-------------

* ``scripts/list_all_docs_repos.sh`` - print every repo currently tagged ``Documentation=true``.
* ``scripts/find_untagged_repos.sh`` - print repos with "docs" in the name that are *not* tagged. Useful for spotting docs sites that need onboarding into the fleet.

Building this site's documentation
==================================

"Building" is the process of running Sphinx to turn the source files in ``docs/`` into a navigable HTML site.

There are three ways to build:

* Locally via ``sphinx-autobuild``
* Automatically via ReadTheDocs
* Inside VS Code, using the supplied devcontainer

Using ReadTheDocs
-----------------

ReadTheDocs builds automatically when a Pull Request is opened, when new commits are pushed to an open PR, and when a PR is merged.

Local live preview
------------------

Assuming a Unix-based system:

.. code-block:: bash

  # Make sure you have python3 venv, e.g. for Ubuntu
  # If you're not sure, try creating a venv, and see if it errors
  sudo apt-get install python3-venv

  # Create and enter a venv
  python3 -m venv .ve
  source .ve/bin/activate

  # Install requirements
  pip install -r requirements_dev.txt

  # Run sphinx-autobuild
  sphinx-autobuild docs docs/_build/html

Then go to http://localhost:8000/ in a browser. Saved changes update the browser automatically. To change the language, edit the ``language`` variable in ``docs/conf.py``.

Using VS Code
-------------

A ``.devcontainer/devcontainer.json`` and ``.vscode/launch.json`` are supplied which add ``sphinx-autobuild`` as a Run option.

Contributing
============

Create a branch, make your changes, and open a Pull Request. ReadTheDocs will build a preview so you can see what the site will look like once merged.

Formatting
----------

Python code in this repo is formatted with `black <https://github.com/psf/black>`_. The project is configured to format on save in VS Code; run ``black .`` to format manually.

Translations
============

The process for getting documentation translated is:

* Extract English strings into a ``.pot`` file
* Send the ``.pot`` file for translation
* Receive ``.po`` files from the translation process
* Check the ``.po`` files into the repo
* Re-run the build with the translations

Extract strings
---------------

.. code-block:: bash

  cd docs
  make gettext
  # .pot files are in _build/locale

Send for translation & receive translations
-------------------------------------------

There is no automation for this step. Contact @robredpath for the current process.

Check the files into the repo
-----------------------------

Place the files into ``docs/locale/<lang>/LC_MESSAGES/`` (e.g. ``fr`` for French).

Re-run the build
----------------

On ReadTheDocs, translation projects do **not** auto-build on Pull Request. To preview a translation, create a Version via the RTD interface and point it at your branch. Translated versions rebuild automatically when the PR is merged.

To build a translation locally:

.. code-block:: bash

  cd docs
  make -e SPHINXOPTS="-D language='fr'" dirhtml

Built docs are in ``docs/_build/dirhtml``.
