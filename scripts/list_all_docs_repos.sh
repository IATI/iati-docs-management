#!/bin/bash
gh api /orgs/IATI/properties/values --paginate --jq '.[] | select(.properties[] | select(.property_name == "Documentation" and .value == "true")) | .repository_name' | cat
