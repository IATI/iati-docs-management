#!/bin/bash
# Find repos with "docs" in the name that don't have Documentation property set to true

# Get all repos with Documentation=true
tagged_repos=$(gh api /orgs/IATI/properties/values --paginate --jq '.[] | select(.properties[] | select(.property_name == "Documentation" and .value == "true")) | .repository_name')

# Get all repos with "docs" in the name (case insensitive)
gh api --paginate /orgs/IATI/repos --jq '.[].name' | grep -i docs | while read -r repo; do
    if ! echo "$tagged_repos" | grep -qx "$repo"; then
        echo "$repo"
    fi
done
