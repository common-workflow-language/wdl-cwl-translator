---
title: 'wdl2cwl: A Python tool for converting WDL workflows to CWL'
tags:
  - Python
  - workflow
  - workflow description language
  - common workflow language
authors:
  - name: Dinithi Wickramaratne
  - name: Dennis Chukwunta
  - name: Michael R. Crusoe
    orcid: 0000-0002-2961-9670
  - name: Bruno P. Kinoshita
    orcid: 0000-0001-8250-4074
  - name: Peter Amstutz
    orcid: 0000-0003-3566-7705
date:  August 2022
bibliography: paper.bib
---

# Summary

Computational workflows are used in data analysis, enabling innovation and 
decision-making in various fields of science. There are several competing workflow description systems,
WDL (Workflow description Language) and CWL (Common Workflow Language) are two examples
of popularly used systems. WDL offers a way to specify data processing workflows with 
a human-readable and writable syntax. WDL makes it straightforward to define 
complex analysis tasks, chain them together in workflows, and parallelize their execution [@wdl].
CWL on the other hand, is a way to describe command line tools and connect 
them together to create workflows. Because CWL is a specification and not a 
specific pieces of software, tools and workflows described using CWL are portable across
a variety of platforms that support the CWL standard [@cwl].

Groups using WDL or CWL can find it difficult to learn the other workflow definition
language if they need to run their workflows in other platforms. It becomes even harder 
when the workflows contain domain specific knowledge mixed with the definitions, and 
when analysts and scientists need to consider future collaboration of other groups on 
the workflows definitions.

``wdl2cwl`` is a Python package for converting workflows written in WDL to workflows in CWL.
The ``wdl2cwl`` package uses the ``miniwdl`` package [@miniwdl], an open source, Python package, 
developer toolkit and local runner for running WDL files. The ``wdl2cwl`` package was
designed to provide a class-based and developer-friendly interface for extracting 
the various sections of a WDL file, dynamically create the various objects
needed to create a valid CWL file ( this is done using the ``cwl-utils`` package [@cwl_utils], 
an open source, Python package for parsing CWL files) and performing error checking 
for valid imports, input declarations, expressions and other useful operations on CWL files.

``wdl2cwl`` was designed to be used by both CWL/WDL professionals and researchers, as well
as students of bioinformatics and data analytics. It can help CWL analysts to avoid
platform dependency issues (like trying to reproduce a WDL workflow on a platform that 
only allows CWL). It can help analysts by eliminating the difficulty of learning a new
workflow language before they can use a WDL workflow. It can be used to create a more 
widely available workflow that can be used by analysts of either language.

# Acknowledgements

We acknowledge the Chan Zuckerberg Initiative (CZI) for supporting this work through their EOSS - Diversity & Inclusion grant. We are grateful to Outreachy and their internship program for organizing and providing support for two internships (six months) focused on building this project. Additionally, we acknowledge the work by the developers of miniwdl, CWL and WDL.

# References
