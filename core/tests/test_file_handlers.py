# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from django.test import SimpleTestCase
import unittest.mock
from core.models import File
from core.services.file_handlers.factory import FileHandlerFactory
from core.services.file_handlers.python import PythonHandler
from core.services.file_handlers.java import JavaHandler
from core.services.file_handlers.node import NodeHandler
from core.services.file_handlers.notebook import NotebookHandler

class FileHandlerTests(SimpleTestCase):
    
    def test_factory_python(self):
        f = File(name="test.py", extension="py", data="import os")
        handler = FileHandlerFactory.get_handler(f)
        self.assertIsInstance(handler, PythonHandler)
        self.assertEqual(handler.get_language(), "python-3.12")
        
    def test_python_requirements(self):
        code = "import numpy\nfrom pandas import DataFrame"
        f = File(name="test.py", extension="py", data=code)
        handler = FileHandlerFactory.get_handler(f)
        reqs = handler.get_requirements()
        self.assertIn("numpy", reqs)
        self.assertIn("pandas", reqs)
        
    def test_factory_java(self):
        f = File(name="Test.java", extension="java", data="import java.util.List;")
        handler = FileHandlerFactory.get_handler(f)
        self.assertIsInstance(handler, JavaHandler)
        self.assertEqual(handler.get_language(), "java-27")

    def test_java_requirements_pom(self):
        # Test pom generation from imports. org.junit now maps to the baked
        # JUnit 5/6 Jupiter artifact (not legacy junit:junit 4).
        code = "import org.junit.jupiter.api.Test;"
        f = File(name="Test.java", extension="java", data=code)
        handler = FileHandlerFactory.get_handler(f)
        reqs = handler.get_requirements()
        self.assertIn("<groupId>org.junit.jupiter</groupId>", reqs)
        self.assertIn("<artifactId>junit-jupiter</artifactId>", reqs)

    def test_node_requirements(self):
        code = "import axios from 'axios';"
        f = File(name="test.js", extension="js", data=code)
        
        # Mock the scanning logic to avoid dependency on esprima being installed
        with unittest.mock.patch('core.services.file_handlers.node.NodeHandler.scan_content', return_value={'axios'}):
            handler = FileHandlerFactory.get_handler(f)
            self.assertIsInstance(handler, NodeHandler)
            reqs = handler.get_requirements()
            
            self.assertIsNotNone(reqs)
            self.assertIn("axios", reqs)
            self.assertIn("dependencies", reqs.lower())

    def test_notebook_delegation_python(self):
        # Mock a python notebook
        nb_content = '''
        {
         "metadata": {
          "kernelspec": {
           "display_name": "Python 3",
           "language": "python",
           "name": "python3"
          }
         },
         "cells": [
          {
           "cell_type": "code",
           "source": ["import numpy"]
          }
         ]
        }
        '''
        f = File(name="test.ipynb", extension="ipynb", data=nb_content)
        handler = FileHandlerFactory.get_handler(f)
        self.assertIsInstance(handler, NotebookHandler)
        self.assertEqual(handler.get_language(), "python-3.12")
        
        reqs = handler.get_requirements()
        self.assertIn("numpy", reqs)

    def test_notebook_delegation_r(self):
        # Mock an R notebook
        nb_content = '''
        {
         "metadata": {
          "kernelspec": {
           "language": "R",
           "name": "ir"
          }
         },
         "cells": [
          {
           "cell_type": "code",
           "source": ["library(ggplot2)"]
          }
         ]
        }
        '''
        f = File(name="test.ipynb", extension="ipynb", data=nb_content)
        handler = FileHandlerFactory.get_handler(f)
        self.assertEqual(handler.get_language(), "r-4")
        
        reqs = handler.get_requirements()
        self.assertIn("ggplot2", reqs)
