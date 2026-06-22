====================================
Creating A New Documentation Website
====================================

.. note::

   Before starting, ensure that you have permission to create new repos in the IATI GitHub organisation and are an Owner of the IATI ReadTheDocs organisation. Talk to Rob or `a GitHub organisation owner <https://github.com/orgs/IATI/people>`_ if you're not sure. 

Create the repo & configure the site
====================================

Go to https://github.com/IATI/iati-docs-base/tree/main and click the "Use this template" button on the repo to create a new repo.

Then, go through docs/project_info.py and update with all the details of whatever you're documenting. 

Set up ReadTheDocs
==================

.. attention::

   Ensure that you log in using GitHub rather than Google or username and password. Permissions for projects follow your GitHub permissions automatically. 


Log in to `app.readthedocs.com <https://app.readthedocs.com/dashboard/>`_ . 

Once logged in, click Add Project and follow through the flow to add the project. You can ignore a banner saying "Failed to add deploy key to GitHub project, ensure you have the correct permissions and try importing again.", if it does appear for you. 

Repeat the Add Project flow again for each language that you're adding translations for, using the same repo and following the convention of appending -fr/-es etc at the end of each project name, and setting the Language of the project to the appropriate value. 

Then, go to the Settings of the English version of the docs, click "translations" in the menu, and add the extra projects you just created as Translations of the first. 

Finally, go through each of the projects that you've just created and make them public. Start in Settings, and ensure that the Privacy Level is set to Public and that the "Build pull requests for this project" box is checked. Then, go to Versions -> latest and set the Privacy Level to Public there as well. 

Set up the domain
=================

This is to have your project appear at docs.whatever.iatistandard.org:

Go to the ReadTheDocs project for the English language version of the project. Under the Hosting -> Domains menu item, enter the domain that you want the project to appear at and check the "Canonical" box. Click Save, and keep a note of the "CNAME Target" displayed.

Ask someone with access to Cloudflare (any IATI developer, including Rob) to set up DNS for you. They will need the CNAME Target from earlier, and the domain that you want the project to live on. 

Once they've done their work, give it 10 minutes to all sync up and settle down, then it should be working. 


Write your content
==================

You're now ready to write your content - see other documentation sites for examples of layouts and structure, and look at the "kitchen sink" for examples of Sphinx features. All content must be written in `ReStructuredText <https://www.sphinx-doc.org/en/master/usage/restructuredtext/basics.html>`_ .

Don't forget to remove the Kitchen Sink before you share documentation. 