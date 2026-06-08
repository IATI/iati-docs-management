======================
Updating Documentation
======================

In order to update documentation, you need to:

* Get the latest version of the documentation
* Make your updates & preview them
* Open a Pull Request to seek review from colleagues and test that they work
* Merge the Pull Request to update the live website. 

Get the latest version 
======================

Clone the relevant GitHub repo and check out the main branch. Or, if you've already got it, git pull. 

Then, create a new branch to work on. 

Make your edits & preview your work
===================================

As you write, it's helpful to see how your work will look once it's live. 

There are three ways to do this: locally with VS Code, locally without VS Code, or on ReadTheDocs

Locally (with VS Code)
----------------------

All IATI documentation sites include devcontainer configuration which closely replicates the ReadTheDocs server environment. 

Open the directory that contains the repo, and re-open it in a devcontainer. Once built, go to the debugging interface and start the "Sphinx Autobuild" debugger. You can see any build errors in the built-in console and view the documentation at http://127.0.0.1:8000/

Locally (without VS Code)
-------------------------

Assuming a unix based system (e.g. MacOS, Linux):

.. code-block:: bash

  # Make sure you have python3 venv, e.g. for Ubuntu
  # If you're not sure, try creating a venv, and see if it errors
  sudo apt-get install python3-venv
  
  # Create a venv
  python3 -m venv .ve    
  
  # Enter the venv, needs to be run for every new shell
  source .ve/bin/activate
  
  # Install requirements
  pip install -r requirements_dev.txt
  
  # Run sphinx-autobuild
  sphinx-autobuild -b dirhtml docs docs/_build/html

Then go to http://localhost:8000/ in a browser.
When you save changes to a file, it should update in the browser automatically.
To change the language, edit the `language` variable in `docs/conf.py`.

Using ReadTheDocs
-----------------

ReadTheDocs will automatically build when a new Pull Request is opened on GitHub, whenever a new commit is pushed to an open Pull Request, or when a Pull Request is merged. 


Open a Pull Request
===================

Open a GitHub Pull Request from your branch to main. Check that the automatic build succeeds, and that the site looks as you expect. Then, ask any colleagues that you want to review your work. 

It's best practice to be specific with your requests for review. 

Merge the Pull Request
======================

Once the PR has been reviewed and approved by a colleague, you can merge it. 