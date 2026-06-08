===========
Translation
===========

Automated Translation
=====================

IATI's approach to machine translation is evolving; see the `documentation translation prototype <https://github.com/IATI/documentation-translation-prototype>`_ on GitHub for the latest work.

Automated translation is currently available from English into French and Spanish. Additional languages can be added, but this requires effort to build up the necessary resources first. 

For most websites, automatic translation is suitable: the benefits of instant low-cost translation outweigh the risk of poor translation. 

Manual Translation
==================

For any application where manual translation is required, speak to Rob about accessing human translation services. 

The process for getting documentation translated is:

* Extract English strings into a .pot file
* Send the .pot files to an agency for translation
* Recieve .po files from the translation agency
* Check the .po files into the repo
* Re-run the build process with the translations

Extract Strings
---------------

.. code-block:: bash

  cd docs
  make gettext
  # .pot files are in _build/locale

Send for translation & Receive translations
-------------------------------------------

Nothing automated here, sorry! This is carried out entirely via email. 

Check the files into the repo
-----------------------------

Place the files into `docs/locale/fr/LC_MESSAGES/` (replacing fr with the appropriate language code as required)

Re-run the build
----------------

On ReadTheDocs, projects that are translations don't auto-build on Pull Request. If you want to preview the documentation in another language, you can create a Version via the RTD interface and set it up to build the branch that you're working on. Translated versions will automatically rebuild when the Pull Request is merged, however. 

If building locally: 

.. code-block:: bash

  cd docs
  make -e SPHINXOPTS="-D language='fr'" dirhtml

Built docs are in `docs/_build/dirhtml`.
