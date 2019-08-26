#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of Beremiz
# Copyright (C) 2019: Edouard TISSERANT
#
# See COPYING file for copyrights details.

from __future__ import absolute_import
import os
import shutil
from itertools import izip, imap
from pprint import pprint, pformat

import wx

import util.paths as paths
from POULibrary import POULibrary
from docutil import open_svg, get_inkscape_path
from lxml import etree

from util.ProcessLogger import ProcessLogger
from runtime.typemapping import DebugTypesSize
import targets

HMI_TYPES_DESC = {
    "HMI_CLASS":{},
    "HMI_LABEL":{},
    "HMI_STRING":{},
    "HMI_INT":{},
    "HMI_REAL":{}
}

HMI_TYPES = HMI_TYPES_DESC.keys()

from XSLTransform import XSLTransform

ScriptDirectory = paths.AbsDir(__file__)

class HMITreeNode(object):
    def __init__(self, path, name, nodetype, iectype = None, vartype = None):
        self.path = path
        self.name = name
        self.nodetype = nodetype

        if iectype is not None:
            self.iectype = iectype
            self.vartype = vartype
        if nodetype in ["HMI_LABEL", "HMI_ROOT"]:
            self.children = []

    def pprint(self, indent = 0):
        res = ">"*indent + pformat(self.__dict__, indent = indent, depth = 1) + "\n"
        if hasattr(self, "children"): 
            res += "\n".join([child.pprint(indent = indent + 1)
                              for child in self.children])
            res += "\n"
            
        return res

    def place_node(self, node):
        best_child = None
        known_best_match = 0
        for child in self.children : 
            if child.path is not None:
                in_common = 0
                for child_path_item, node_path_item in izip(child.path, node.path):
                    if child_path_item == node_path_item:
                        in_common +=1
                    else:
                        break
                if in_common > known_best_match:
                    known_best_match = in_common
                    best_child = child
        if best_child is not None and best_child.nodetype == "HMI_LABEL":
            best_child.place_node(node)
        else:
            self.children.append(node)
            
    def etree(self):

        attribs = dict(name=self.name)
        if self.path is not None:
            attribs["path"]=".".join(self.path)

        res = etree.Element(self.nodetype, **attribs)

        if hasattr(self, "children"): 
            for child_etree in imap(lambda c:c.etree(), self.children):
                res.append(child_etree)

        return res

    def traverse(self):
        yield self
        if hasattr(self, "children"): 
            for c in self.children:
                for yoodl in c.traverse():
                    yield yoodl

# module scope for HMITree root
# so that CTN can use HMITree deduced in Library
# note: this only works because library's Generate_C is 
#       systematicaly invoked before CTN's CTNGenerate_C

hmi_tree_root = None

class SVGHMILibrary(POULibrary):
    def GetLibraryPath(self):
         return paths.AbsNeighbourFile(__file__, "pous.xml")

    def Generate_C(self, buildpath, varlist, IECCFLAGS):
        global hmi_tree_root

        """
        PLC Instance Tree:
          prog0
           +->v1 HMI_INT
           +->v2 HMI_INT
           +->fb0 (type mhoo)
           |   +->va HMI_LABEL
           |   +->v3 HMI_INT
           |   +->v4 HMI_INT
           |
           +->fb1 (type mhoo)
           |   +->va HMI_LABEL
           |   +->v3 HMI_INT
           |   +->v4 HMI_INT
           |
           +->fb2
               +->v5 HMI_IN

        HMI tree:
          hmi0
           +->v1
           +->v2
           +->fb0_va
           |   +-> v3
           |   +-> v4
           |
           +->fb1_va
           |   +-> v3
           |   +-> v4
           |
           +->v5

        """

        # Filter known HMI types
        hmi_types_instances = [v for v in varlist if v["derived"] in HMI_TYPES]

        hmi_tree_root = HMITreeNode(None, "/", "HMI_ROOT")

        # add special nodes 
        map(lambda (n,t): hmi_tree_root.children.append(HMITreeNode(None,n,t)), [
                ("plc_status", "HMI_PLC_STATUS"),
                ("current_page", "HMI_CURRENT_PAGE")])

        # deduce HMI tree from PLC HMI_* instances
        for v in hmi_types_instances:
            path = v["C_path"].split(".")
            # ignores variables starting with _TMP_
            if path[-1].startswith("_TMP_"):
                continue
            new_node = HMITreeNode(path, path[-1], v["derived"], v["type"], v["vartype"])
            hmi_tree_root.place_node(new_node)

        print(hmi_tree_root.pprint())

        variable_decl_array = []
        extern_variables_declarations = []
        buf_index = 0
        for node in hmi_tree_root.traverse():
            if hasattr(node, "iectype"):
                sz = DebugTypesSize.get(node.iectype, 0)
                variable_decl_array += [
                    "{&(" + ".".join(node.path) + "), " + node.iectype + {
                        "EXT": "_P_ENUM",
                        "IN":  "_P_ENUM",
                        "MEM": "_O_ENUM",
                        "OUT": "_O_ENUM",
                        "VAR": "_ENUM"
                    }[node.vartype] + ", " +
                    str(buf_index) + ", 0}"]
                buf_index += sz
                if len(node.path) == 1:
                    extern_variables_declarations += [
                        "extern __IEC_" + node.iectype + "_" +
                        "t" if node.vartype is "VAR" else "p"
                        + ".".join(node.path) + ";"]

        # TODO : filter only requiered external declarations
        for v in varlist :
            if v["C_path"].find('.') < 0 and v["vartype"] == "FB" :
                extern_variables_declarations += [
                    "extern %(type)s %(C_path)s;" % v]

        # TODO check if programs need to be declared separately
        # "programs_declarations": "\n".join(["extern %(type)s %(C_path)s;" %
        #                                     p for p in self._ProgramList]),

        # TODO generate C code to observe/access HMI tree variables
        svghmi_c_filepath = paths.AbsNeighbourFile(__file__, "svghmi.c")
        svghmi_c_file = open(svghmi_c_filepath, 'r')
        svghmi_c_code = svghmi_c_file.read()
        svghmi_c_file.close()
        svghmi_c_code = svghmi_c_code % { 
            "variable_decl_array": ",\n".join(variable_decl_array),
            "extern_variables_declarations": "\n".join(extern_variables_declarations),
            "buffer_size": buf_index,
            "var_access_code": targets.GetCode("var_access.c")
            }

        gen_svghmi_c_path = os.path.join(buildpath, "svghmi.c")
        gen_svghmi_c = open(gen_svghmi_c_path, 'w')
        gen_svghmi_c.write(svghmi_c_code)
        gen_svghmi_c.close()

        return (["svghmi"], [(gen_svghmi_c_path, IECCFLAGS)], True), ""

class SVGHMI(object):
    XSD = """<?xml version="1.0" encoding="ISO-8859-1" ?>
    <xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema">
      <xsd:element name="SVGHMI">
        <xsd:complexType>
          <xsd:attribute name="enableHTTP" type="xsd:boolean" use="optional" default="false"/>
          <xsd:attribute name="bindAddress" type="xsd:string" use="optional" default="localhost"/>
          <xsd:attribute name="port" type="xsd:string" use="optional" default="8080"/>
        </xsd:complexType>
      </xsd:element>
    </xsd:schema>
    """

    ConfNodeMethods = [
        {
            "bitmap":    "ImportSVG",
            "name":    _("Import SVG"),
            "tooltip": _("Import SVG"),
            "method":   "_ImportSVG"
        },
        {
            "bitmap":    "ImportSVG",  # should be something different
            "name":    _("Inkscape"),
            "tooltip": _("Edit HMI"),
            "method":   "_StartInkscape"
        },
    ]

    def _getSVGpath(self, project_path=None):
        if project_path is None:
            project_path = self.CTNPath()
        # define name for SVG file containing gui layout
        return os.path.join(project_path, "gui.svg")


    def OnCTNSave(self, from_project_path=None):
        if from_project_path is not None:
            shutil.copyfile(self._getSVGpath(from_project_path),
                            self._getSVGpath())
        return True

    def GetSVGGeometry(self):
        # invoke inskscape -S, csv-parse output, produce elements
        InkscapeGeomColumns = ["Id", "x", "y", "w", "h"]

        # TODO : move following line to __init__
        inkpath = get_inkscape_path()
        svgpath = self._getSVGpath()
        _status, result, _err_result = ProcessLogger(None,
                                                     inkpath + " -S " + svgpath,
                                                     no_stdout=True,
                                                     no_stderr=True).spin()
        res = []
        for line in result.split():
            strippedline = line.strip()
            attrs = dict(
                zip(InkscapeGeomColumns, line.strip().split(',')))

            res.append(etree.Element("bbox", **attrs))

        return res

    def GetHMITree(self):
        global hmi_tree_root
        res = [hmi_tree_root.etree()]
        return res

    def CTNGenerate_C(self, buildpath, locations):
        """
        Return C code generated by iec2c compiler
        when _generate_softPLC have been called
        @param locations: ignored
        @return: [(C_file_name, CFLAGS),...] , LDFLAGS_TO_APPEND
        """

        svgfile = self._getSVGpath()
        if os.path.exists(svgfile):

            # TODO : move to __init__
            transform = XSLTransform(os.path.join(ScriptDirectory, "gen_index_xhtml.xslt"),
                          [("GetSVGGeometry", lambda *_ignored:self.GetSVGGeometry()),
                           ("GetHMITree", lambda *_ignored:self.GetHMITree())])


            # load svg as a DOM with Etree
            svgdom = etree.parse(svgfile)

            # call xslt transform on Inkscape's SVG to generate XHTML
            result = transform.transform(svgdom)
           
            # print(str(result))
            # print(transform.xslt.error_log)

            # TODO
            #   - Errors on HMI semantics
            #   - ... maybe something to have a global view of what is declared in SVG.

        else:
            # TODO : use default svg that expose the HMI tree as-is 
            pass


        res = ([], "", False)

        targetpath = os.path.join(self._getBuildPath(), "target.xhtml")
        targetfile = open(targetpath, 'w')

        # TODO : DOM to string
        targetfile.write("TODO")
        targetfile.close()
        res += (("target.js", open(targetpath, "rb")),)

        # TODO add C code to expose HMI tree variables to shared memory
        # TODO generate a description of shared memory (xml or CSV) 
        #      that can be loaded by svghmi QTWeb* app or svghmi server


        return res

    def _ImportSVG(self):
        dialog = wx.FileDialog(self.GetCTRoot().AppFrame, _("Choose a SVG file"), os.getcwd(), "",  _("SVG files (*.svg)|*.svg|All files|*.*"), wx.OPEN)
        if dialog.ShowModal() == wx.ID_OK:
            svgpath = dialog.GetPath()
            if os.path.isfile(svgpath):
                shutil.copy(svgpath, self._getSVGpath())
            else:
                self.GetCTRoot().logger.write_error(_("No such SVG file: %s\n") % svgpath)
        dialog.Destroy()

    def _StartInkscape(self):
        svgfile = self._getSVGpath()
        open_inkscape = True
        if not self.GetCTRoot().CheckProjectPathPerm():
            dialog = wx.MessageDialog(self.GetCTRoot().AppFrame,
                                      _("You don't have write permissions.\nOpen Inkscape anyway ?"),
                                      _("Open Inkscape"),
                                      wx.YES_NO | wx.ICON_QUESTION)
            open_inkscape = dialog.ShowModal() == wx.ID_YES
            dialog.Destroy()
        if open_inkscape:
            if not os.path.isfile(svgfile):
                svgfile = None
            open_svg(svgfile)
