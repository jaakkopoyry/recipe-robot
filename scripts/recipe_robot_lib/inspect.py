#!/usr/bin/python
# This Python file uses the following encoding: utf-8

# Recipe Robot
# Copyright 2015-2019 Elliot Jordan, Shea G. Craig, and Eldon Ahrold
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
inspect.py

Look at a path or URL for an app and generate facts about it.
"""


from __future__ import absolute_import
import httplib
import json
import os
import re
import shutil
import sys
from distutils.version import LooseVersion, StrictVersion
from ssl import CertificateError, SSLError
from urllib2 import HTTPError, Request, URLError, urlopen
from urlparse import urlparse
from xml.etree.ElementTree import ParseError, parse

import xattr
from recipe_robot_lib import FoundationPlist as FoundationPlist
from recipe_robot_lib.exceptions import RoboError
from recipe_robot_lib.tools import (
    ALL_SUPPORTED_FORMATS,
    CACHE_DIR,
    SUPPORTED_ARCHIVE_FORMATS,
    SUPPORTED_BUNDLE_TYPES,
    SUPPORTED_IMAGE_FORMATS,
    SUPPORTED_INSTALL_FORMATS,
    LogLevel,
    any_item_in_string,
    get_exitcode_stdout_stderr,
    robo_print,
)


def process_input_path(facts):
    """Determine which functions to call based on type of input path.

    Args:
        args: The command line arguments.
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
    """
    args = facts["args"]
    # If --config was specified without an input path, stop here.
    if not args.input_path:
        sys.exit(0)
    # Otherwise, retrieve the input path.
    input_path = args.input_path
    robo_print("Processing: {}".format(input_path))

    # Strip trailing slash, but only if input_path is not a URL.
    if "http" not in input_path:
        input_path = input_path.rstrip("/ ")

    # Put input_path into our facts for reporting purposes
    facts["input_path"] = input_path

    # Initialize facts that are lists.
    facts["inspections"] = []
    facts["blocking_applications"] = []
    facts["codesign_authorities"] = []

    github_domains = ("github.com", "githubusercontent.com", "github.io")

    # Determine what kind of input path we are working with, then
    # inspect it.
    inspect_func = None
    if input_path.lower().startswith("http"):
        if (
            input_path.lower().endswith((".xml", ".rss", ".php"))
            or "appcast" in input_path.lower()
        ):
            robo_print("Input path looks like a Sparkle feed.", LogLevel.VERBOSE)
            inspect_func = inspect_sparkle_feed_url
        elif any_item_in_string(github_domains, input_path.lower()):
            robo_print("Input path looks like a GitHub URL.", LogLevel.VERBOSE)
            if "/download/" in input_path.lower():
                facts["warnings"].append(
                    "I'm processing the input path as a GitHub repo URL, but you may "
                    "have wanted me to treat it as a download URL."
                )
            inspect_func = inspect_github_url
        elif "sourceforge.net" in input_path.lower():
            robo_print("Input path looks like a SourceForge URL.", LogLevel.VERBOSE)
            inspect_func = inspect_sourceforge_url
        elif "bitbucket.org" in input_path.lower():
            robo_print("Input path looks like a BitBucket URL.", LogLevel.VERBOSE)
            if "/downloads/" in input_path.lower():
                facts["warnings"].append(
                    "I'm processing the input path as a BitBucket repo URL, but you "
                    "may have wanted me to treat it as a download URL."
                )
            inspect_func = inspect_bitbucket_url
        elif "dropbox.com/s/" in input_path.lower():
            robo_print("Input path looks like a Dropbox shared link.", LogLevel.VERBOSE)
            # Configure the shared link to force file download.
            input_path = input_path.replace("?dl=0", "?dl=1")
            inspect_func = inspect_download_url
        else:
            robo_print("Input path looks like a download URL.", LogLevel.VERBOSE)
            inspect_func = inspect_download_url
    elif input_path.lower().startswith("ftp"):
        robo_print("Input path looks like a download URL.", LogLevel.VERBOSE)
        inspect_func = inspect_download_url
    elif os.path.exists(input_path):
        if input_path.endswith(".app"):
            robo_print("Input path looks like an app.", LogLevel.VERBOSE)
            inspect_func = inspect_app
        elif input_path.endswith(".recipe"):
            raise RoboError("Sorry, I can't use existing AutoPkg recipes as input.")
        elif input_path.endswith(SUPPORTED_INSTALL_FORMATS):
            robo_print("Input path looks like an installer.", LogLevel.VERBOSE)
            inspect_func = inspect_pkg
        elif input_path.endswith(SUPPORTED_IMAGE_FORMATS):
            robo_print("Input path looks like a disk image.", LogLevel.VERBOSE)
            inspect_func = inspect_disk_image
        elif input_path.endswith(SUPPORTED_ARCHIVE_FORMATS):
            robo_print("Input path looks like an archive.", LogLevel.VERBOSE)
            inspect_func = inspect_archive
        else:
            raise RoboError(
                "I haven't been trained on how to handle this input path:\n"
                "\t%s" % input_path
            )
    else:
        raise RoboError(
            "Input path does not exist. Please try again with a valid input path."
        )

    if inspect_func:
        facts = inspect_func(input_path, args, facts)


def check_url(url):
    """Test a URL's headers, and switch to HTTPS if available.

    Args:
        url: The URL to check.

    Returns:
        url: The URL that was tested, which may not be the same as the URL
            provided as input (e.g. switching from HTTP to HTTPS).
        code: The HTTP status code returned by the URL header check.
    """
    prs = urlparse(url)
    # TODO (Elliot): Support URLs with hard-coded port other than 80/443.
    if prs.scheme == "https" or ":" in prs.netloc:
        return url
    elif prs.scheme == "http":
        robo_print("Checking for HTTPS URL...", LogLevel.VERBOSE)
        try:
            # Try switching to HTTPS.
            cnx = httplib.HTTPSConnection(prs.netloc, 443, timeout=10)
            cnx.request("HEAD", prs.path)
            res = cnx.getresponse()
            if res.status < 400:
                url = "https" + url[4:]
                robo_print("Found HTTPS URL: %s" % url, LogLevel.VERBOSE, 4)
                return url
            else:
                robo_print("No usable HTTPS URL found.", LogLevel.VERBOSE, 4)
        except (CertificateError, SSLError) as err:
            robo_print(
                "Domain does not have a valid SSL certificate.", LogLevel.VERBOSE, 4
            )
        except Exception as err:  # pylint: disable=W0703
            robo_print(
                "An error occurred while checking for an HTTPS URL: %s" % err,
                LogLevel.VERBOSE,
                4,
            )

    # Use HTTP if HTTPS fails.
    cnx = httplib.HTTPConnection(prs.netloc, 80, timeout=10)  # pylint: disable=R0204
    cnx.request("HEAD", prs.path)
    res = cnx.getresponse()
    # TODO (Elliot): Mitigation of errors based on res.status.

    return url


def inspect_app(input_path, args, facts, bundle_type="app"):
    """Process an app

    Gather information required to create a recipe.

    Args:
        input_path: The path or URL that Recipe Robot was asked to use
            to create recipes.
        args: The command line arguments.
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        bundle_type: Type or file extension of the bundle we're inspecting.
            Typically "app" but we also support "prefpane" and others.

    Returns:
        facts dictionary.
    """
    # Only proceed if we haven't inspected this app yet.
    if bundle_type in facts["inspections"]:
        return facts
    else:
        facts["inspections"].append(bundle_type)

    # Save the path of the app. (Used when overriding AppStoreApp
    # recipes.)
    facts["app_path"] = input_path

    # Record this app as a blocking application (for munki recipe based
    # on pkg).
    if bundle_type == "app":
        facts["blocking_applications"].append(os.path.basename(input_path))

    # Read the app's Info.plist.
    robo_print("Validating {}...".format(bundle_type), LogLevel.VERBOSE)
    try:
        info_plist = FoundationPlist.readPlist(input_path + "/Contents/Info.plist")
        robo_print("This {} seems valid".format(bundle_type), LogLevel.VERBOSE, 4)
    except (ValueError, FoundationPlist.NSPropertyListSerializationException) as error:
        raise RoboError(
            "{} doesn't look like a valid {} to me.".format(input_path, bundle_type),
            error,
        )

    # Get the filename of the app (which is usually the same as the app
    # name.)
    app_file = os.path.splitext(os.path.basename(input_path))[0]

    # Determine the name of the app. (Overwrites any previous app_name,
    # because the app Info.plist itself is the most reliable source.)
    if bundle_type == "app" or (
        bundle_type != "app" and "app" not in facts["inspections"]
    ):
        app_name = ""
        robo_print("Getting bundle name...", LogLevel.VERBOSE)
        if "CFBundleName" in info_plist:
            app_name = info_plist["CFBundleName"]
        elif "CFBundleExecutable" in info_plist:
            app_name = info_plist["CFBundleExecutable"]
        else:
            app_name = app_file
        robo_print("Bundle name is: %s" % app_name, LogLevel.VERBOSE, 4)
        facts[bundle_type + "_name"] = app_name

    # If the app's filename is different than the app's name, we need to
    # make a note of that. Many recipes require another input variable
    # for this.
    if app_name != app_file:
        robo_print(
            "Bundle name differs from the actual {} filename.".format(bundle_type),
            LogLevel.VERBOSE,
        )
        robo_print(
            "Actual {0} filename: {1}.{0}".format(bundle_type, app_file),
            LogLevel.VERBOSE,
            4,
        )
        facts["app_file"] = app_file

    # Determine the bundle identifier of the app. (Overwrites any
    # previous bundle_id, because the app itself is the most reliable
    # source.)
    if bundle_type == "app" or (
        bundle_type != "app" and "app" not in facts["inspections"]
    ):
        bundle_id = ""
        robo_print("Getting bundle identifier...", LogLevel.VERBOSE)
        if "CFBundleIdentifier" in info_plist:
            bundle_id = info_plist["CFBundleIdentifier"]
            robo_print("Bundle identifier is: %s" % bundle_id, LogLevel.VERBOSE, 4)
            facts["bundle_id"] = bundle_id
        else:
            facts["warnings"].append(
                "{} doesn't seem to have a bundle identifier.".format(app_name)
            )

    # Leave a hint for people testing Recipe Robot on itself.
    if (
        bundle_type == "app"
        and bundle_id == "com.elliotjordan.recipe-robot"
        and "github_url" not in facts["inspections"]
    ):
        facts["warnings"].append(
            "I see what you did there! Try using my GitHub URL "
            "as input instead of the app itself. You may also "
            "need to use --ignore-existing."
        )

    # Warn if this looks like an installer app.
    if (
        bundle_type == "app"
        and app_name.startswith("Install ")
        or app_name.endswith(" Installer.app")
    ):
        facts["warnings"].append(
            "Heads up! This app looks like an installer app rather than the actual app you "
            "wanted. You may need to manually extract and repackage the app, or download the "
            "app from a different source."
        )

    # Attempt to determine how to download this app.
    if bundle_type == "app" and "sparkle_feed" not in facts:
        sparkle_feed = ""
        download_format = ""
        robo_print("Checking for a Sparkle feed...", LogLevel.VERBOSE)
        if "SUFeedURL" in info_plist:
            sparkle_feed = info_plist["SUFeedURL"]
        elif "SUOriginalFeedURL" in info_plist:
            sparkle_feed = info_plist["SUOriginalFeedURL"]
        elif os.path.exists(
            "{}/Contents/Frameworks/DevMateKit.framework".format(input_path)
        ):
            sparkle_feed = "https://updates.devmate.com/{}.xml".format(bundle_id)
        if sparkle_feed != "" and sparkle_feed != "NULL":
            facts = inspect_sparkle_feed_url(sparkle_feed, args, facts)
        else:
            robo_print("No Sparkle feed", LogLevel.VERBOSE, 4)

    if bundle_type == "app" and "is_from_app_store" not in facts:
        robo_print(
            "Determining whether app was downloaded from the Mac App Store...",
            LogLevel.VERBOSE,
        )
        if os.path.exists("{}/Contents/_MASReceipt/receipt".format(input_path)):
            robo_print("App came from the App Store", LogLevel.VERBOSE, 4)
            facts["is_from_app_store"] = True
        else:
            robo_print("App did not come from the App Store", LogLevel.VERBOSE, 4)
            facts["is_from_app_store"] = False

    # Determine whether to use CFBundleShortVersionString or
    # CFBundleVersion for versioning.
    if "version_key" not in facts:
        version_key = ""
        robo_print("Looking for version key...", LogLevel.VERBOSE)
        if "CFBundleShortVersionString" in info_plist:
            if "CFBundleVersion" in info_plist:
                # Both keys exist, so we must decide with a cage match!
                try:
                    if StrictVersion(info_plist["CFBundleShortVersionString"]):
                        # CFBundleShortVersionString is strict. Use it.
                        version_key = "CFBundleShortVersionString"
                except ValueError:
                    # CFBundleShortVersionString is not strict.
                    try:
                        if StrictVersion(info_plist["CFBundleVersion"]):
                            # CFBundleVersion is strict. Use it.
                            version_key = "CFBundleVersion"
                    except ValueError:
                        # Neither are strict versions. Are they
                        # integers?
                        if info_plist["CFBundleShortVersionString"].isdigit():
                            version_key = "CFBundleShortVersionString"
                        elif info_plist["CFBundleVersion"].isdigit():
                            version_key = "CFBundleVersion"
                        else:
                            # CFBundleShortVersionString wins by
                            # default.
                            version_key = "CFBundleShortVersionString"
            else:
                version_key = "CFBundleShortVersionString"
        else:
            if "CFBundleVersion" in info_plist:
                version_key = "CFBundleVersion"
        if version_key not in ("", None):
            robo_print(
                "Version key is: {} ({})".format(version_key, info_plist[version_key]),
                LogLevel.VERBOSE,
                4,
            )
            facts["version_key"] = version_key
        else:
            raise RoboError(
                "Sorry, I can't determine which version key to use for this {}.".format(
                    bundle_type
                )
            )

    # Determine path to the app's icon.
    if "icon_path" not in facts and not args.skip_icon:
        icon_path = ""
        robo_print("Looking for icon...", LogLevel.VERBOSE)
        if "CFBundleIconFile" in info_plist:
            icon_path = os.path.join(
                input_path, "Contents", "Resources", info_plist["CFBundleIconFile"]
            )
        else:
            facts["warnings"].append("Can't determine icon.")
        if icon_path not in ("", None):
            robo_print("Icon is: {}".format(icon_path), LogLevel.VERBOSE, 4)
            facts["icon_path"] = icon_path

    # Attempt to get a description of the app.
    if "description" not in facts:
        robo_print("Getting description...", LogLevel.VERBOSE)
        description, source = get_app_description(app_name)
        if description is not None:
            robo_print(
                "Description (from {}): {}".format(source, description),
                LogLevel.VERBOSE,
                4,
            )
            facts["description"] = description
        else:
            robo_print("Can't retrieve description.", LogLevel.VERBOSE, 4)

    # Gather info from code signing attributes, including:
    #    - Code signature verification requirements
    #    - Expected authority names
    #    - Name of developer (according to signing certificate)
    #    - Code signature version (version 1 is obsolete, treated as unsigned)
    if facts.get("codesign_reqs", "") == "" and len(facts["codesign_authorities"]) == 0:
        codesign_reqs = ""
        codesign_authorities = []
        developer = ""
        codesign_version = ""
        robo_print("Gathering code signature information...", LogLevel.VERBOSE)
        cmd = '/usr/bin/codesign --display --verbose=2 -r- "{}"'.format(input_path)
        exitcode, out, err = get_exitcode_stdout_stderr(cmd)
        if exitcode == 0:
            # From stdout:
            reqs_marker = "designated => "
            for line in out.split("\n"):
                if line.startswith(reqs_marker):
                    codesign_reqs = line[len(reqs_marker) :]
            # From stderr:
            authority_marker = "Authority="
            dev_marker = "Authority=Developer ID Application: "
            vers_marker = "Sealed Resources version="
            # The info we need is in stderr.
            for line in err.decode("utf-8").split("\n"):
                if line.startswith(authority_marker):
                    codesign_authorities.append(line[len(authority_marker) :])
                if line.startswith(dev_marker):
                    if " (" in line:
                        line = line.split(" (")[0]
                    developer = line[len(dev_marker) :]
                if line.startswith(vers_marker):
                    codesign_version = line[len(vers_marker) : len(vers_marker) + 1]
                    if codesign_version == "1":
                        facts["warnings"].append(
                            "This {} uses an obsolete code signature.".format(
                                bundle_type
                            )
                        )
                        # Clear code signature markers, treat app as
                        # unsigned.
                        codesign_reqs = ""
                        codesign_authorities = []
                        break
        if codesign_reqs == "" and len(codesign_authorities) == 0:
            robo_print("This {} is not signed".format(bundle_type), LogLevel.VERBOSE, 4)
        else:
            robo_print(
                "Code signature verification requirements recorded", LogLevel.VERBOSE, 4
            )
            facts["codesign_reqs"] = codesign_reqs
            robo_print(
                "{} authority names recorded".format(len(codesign_authorities)),
                LogLevel.VERBOSE,
                4,
            )
            facts["codesign_authorities"] = codesign_authorities
            facts["codesign_input_filename"] = os.path.basename(input_path)
            # Check for overly loose requirements.
            # https://developer.apple.com/library/archive/documentation/Security/Conceptual/CodeSigningGuide/RequirementLang/RequirementLang.html
            if codesign_reqs in (
                "anchor apple generic",
                "anchor trusted",
                "certificate anchor trusted",
            ):
                facts["warnings"].append(
                    "This {}'s code signing designated requirements are set very "
                    "broadly. You may want to politely suggest that the developer "
                    "set a stricter requirement.".format(bundle_type)
                )
        if developer not in ("", None):
            robo_print("Developer: {}".format(developer), LogLevel.VERBOSE, 4)
            facts["developer"] = developer

    return facts


def html_decode(the_string):
    """Given a string, change HTML escaped characters (&gt;) to regular
    characters (>)."""
    html_chars = (
        ("'", "&#39;"),
        ('"', "&quot;"),
        (">", "&gt;"),
        ("<", "&lt;"),
        ("&", "&amp;"),
    )
    for char in html_chars:
        the_string = the_string.replace(char[1], char[0])
    return the_string


def useragent_urlopen(url, useragent):
    """For a given URL, return the urlopen response after adding the GitHub token."""

    req = Request(url)
    if useragent:
        req.add_header("User-agent", useragent)

    return urlopen(req)


def get_app_description(app_name):
    """Use an app's name to generate a description.

    Args:
        app_name: The name of the app that we need to describe.

    Returns:
        description: A string containing a description of the app.
        source: A string containing the source of the description.
    """
    desc_sources = [
        {
            "name": "MacUpdate",
            "pattern": r"=\"shortdescr\">(?P<desc>.*)</span>",
            "url": "https://www.macupdate.com/find/mac/%s" % app_name,
        },
        {
            "name": "AlternativeTo",
            "pattern": r"<div class=\"itemDesc( read-more-box)?\">\s+"
            '<p class="text">(?P<desc>.*)</p>',
            "url": "https://alternativeto.net/browse/search"
            "/?ignoreExactMatch=true&q=%s" % app_name,
        },
    ]
    for source in desc_sources:
        cmd = '/usr/bin/curl --silent "%s"' % source["url"]
        _, out, _ = get_exitcode_stdout_stderr(cmd)
        result = re.search(source["pattern"], out)
        if result:
            description = unicode(html_decode(result.group("desc")), "utf-8")
            return description, source["name"]
    return None, None


def get_download_link_from_xattr(input_path, args, facts):
    """Attempts to derive download URL from the extended attribute (xattr)
    metadata.
    """
    try:
        where_froms_string = xattr.getxattr(
            input_path, "com.apple.metadata:kMDItemWhereFroms"
        )
        where_froms = FoundationPlist.readPlistFromString(where_froms_string)
        if len(where_froms) > 0:
            facts["download_url"] = where_froms[0]
            robo_print(
                "Download URL found in file metadata: %s" % where_froms[0],
                LogLevel.VERBOSE,
                4,
            )
    except KeyError as err:
        robo_print(
            "Unable to derive a download URL from file metadata.", LogLevel.WARNING
        )


def inspect_archive(input_path, args, facts):
    """Process an archive

    Gather information required to create a recipe.

    Args:
        input_path: The path or URL that Recipe Robot was asked to use
            to create recipes.
        args: The command line arguments.
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.

    Returns:
        facts dictionary.
    """
    # Only proceed if we haven't inspected this pkg yet.
    if "archive" in facts["inspections"]:
        return facts
    else:
        facts["inspections"].append("archive")

    # See if we can determine the download URL from the file metadata.
    if "download_url" not in facts:
        get_download_link_from_xattr(input_path, args, facts)

    # Treat the download as a potential archive.
    unpacked_dir = os.path.join(CACHE_DIR, "unpacked")
    if not os.path.exists(unpacked_dir):
        os.mkdir(unpacked_dir)
    archive_cmds = (
        ("zip", '/usr/bin/unzip "{}" -d "{}"'.format(input_path, unpacked_dir)),
        ("tgz", '/usr/bin/tar -zxvf "{}" -C "{}"'.format(input_path, unpacked_dir)),
    )
    for fmt in archive_cmds:
        exitcode, _, _ = get_exitcode_stdout_stderr(fmt[1])
        if exitcode == 0:
            # Confirmed: the download was an archive. Make a note of that.
            robo_print("Successfully unarchived %s" % fmt[0], LogLevel.VERBOSE, 4)
            facts["download_format"] = fmt[0]

            # If the download filename was ambiguous, change it.
            if not facts.get("download_filename", input_path).endswith(
                SUPPORTED_ARCHIVE_FORMATS
            ):
                facts["download_filename"] = "{}.{}".format(
                    facts.get("download_filename", os.path.basename(input_path)), fmt[0]
                )

            # Locate and inspect any apps, non-app bundles, or pkgs on the root level.
            stop_searching_archive = False
            for this_file in os.listdir(unpacked_dir):
                if this_file.lower().endswith(".app"):
                    facts = inspect_app(
                        os.path.join(unpacked_dir, this_file), args, facts
                    )
                    stop_searching_archive = True
                    return facts
                elif this_file.lower().endswith(
                    tuple([x for x in SUPPORTED_BUNDLE_TYPES])
                ):
                    facts = inspect_app(
                        os.path.join(unpacked_dir, this_file),
                        args,
                        facts,
                        bundle_type=os.path.splitext(this_file)[1].lower().lstrip("."),
                    )
                    stop_searching_archive = True
                    return facts
                elif this_file.lower().endswith(SUPPORTED_INSTALL_FORMATS):
                    facts = inspect_pkg(
                        os.path.join(unpacked_dir, this_file), args, facts
                    )
                    stop_searching_archive = True
                    return facts

            # Didn't find an app, prefpane, or pkg on the root level? Look deeper.
            if stop_searching_archive is False:
                for dirpath, dirnames, filenames in os.walk(unpacked_dir):
                    for dirname in dirnames:
                        if dirname.startswith("."):
                            dirnames.remove(dirname)
                        elif dirname.endswith(".app"):
                            facts = inspect_app(
                                os.path.join(dirpath, dirname), args, facts
                            )
                            facts["relative_path"] = (
                                os.path.relpath(os.path.join(dirpath), unpacked_dir)
                                + "/"
                            )
                            return facts
                        elif dirname.endswith(
                            tuple([x for x in SUPPORTED_BUNDLE_TYPES])
                        ):
                            facts = inspect_app(
                                os.path.join(dirpath, dirname),
                                args,
                                facts,
                                bundle_type=os.path.splitext(dirname)[1]
                                .lower()
                                .lstrip("."),
                            )
                            facts["relative_path"] = (
                                os.path.relpath(os.path.join(dirpath), unpacked_dir)
                                + "/"
                            )
                            return facts
                        elif dirname.endswith(".pkg"):  # bundle packages
                            facts = inspect_pkg(
                                os.path.join(dirpath, dirname), args, facts
                            )
                            facts["relative_path"] = (
                                os.path.relpath(os.path.join(dirpath), unpacked_dir)
                                + "/"
                            )
                            return facts
                    for filename in filenames:
                        if filename.endswith(".pkg"):  # flat packages
                            facts = inspect_pkg(
                                os.path.join(dirpath, filename), args, facts
                            )
                            facts["relative_path"] = (
                                os.path.relpath(os.path.join(dirpath), unpacked_dir)
                                + "/"
                            )
                            return facts

            return facts

    robo_print(
        "Unable to unpack this archive: %s\n(You can ignore this message if the "
        "previous attempt to mount the downloaded file as a disk image "
        "succeeded.)" % input_path,
        LogLevel.DEBUG,
    )
    return facts


def find_supported_release(release_array, download_url_key):
    """Given an array of releases, find releases in supported formats."""

    download_format = None
    download_url = None

    for this_format in ALL_SUPPORTED_FORMATS:
        for asset in release_array:
            if asset[download_url_key].endswith(this_format):
                if download_url is None:
                    download_format = this_format
                    download_url = asset[download_url_key]

    return download_format, download_url


def inspect_bitbucket_url(input_path, args, facts):
    """Process a BitBucket URL

    Gather information required to create a recipe.

    Args:
        input_path: The path or URL that Recipe Robot was asked to use
            to create recipes.
        args: The command line arguments.
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.

    Returns:
        facts dictionary.
    """
    # Only proceed if we haven't inspected this BitBucket URL yet.
    if "bitbucket_url" in facts["inspections"]:
        return facts
    else:
        facts["inspections"].append("bitbucket_url")

    # Grab the BitBucket repo path.
    bitbucket_repo = ""
    robo_print("Getting BitBucket repo...", LogLevel.VERBOSE)
    r_obj = re.search(r"(?<=https://bitbucket\.org/)[\w-]+/[\w-]+", input_path)
    if r_obj is not None:
        bitbucket_repo = r_obj.group(0)
    if bitbucket_repo not in ("", None):
        robo_print("BitBucket repo is: %s" % bitbucket_repo, LogLevel.VERBOSE, 4)
        facts["bitbucket_repo"] = bitbucket_repo

        # Use GitHub API to obtain information about the repo and
        # releases.
        repo_api_url = "https://api.bitbucket.org/2.0/repositories/%s" % bitbucket_repo
        releases_api_url = (
            "https://api.bitbucket.org/2.0/repositories/%s/downloads" % bitbucket_repo
        )
        try:
            raw_json_repo = urlopen(repo_api_url).read()
            parsed_repo = json.loads(raw_json_repo)
            raw_json_release = urlopen(releases_api_url).read()
            parsed_release = json.loads(raw_json_release)
        except HTTPError as err:
            if err.code == 403:
                facts["warnings"].append(
                    "Error occurred while getting information from the BitBucket API. "
                    "If you've been creating a lot of recipes quickly, you may have "
                    "hit the rate limit. Give it a few minutes, then try again. "
                    "(%s)" % err
                )
                return facts
            if err.code == 404:
                facts["warnings"].append("BitBucket API URL not found. (%s)" % err)
                return facts
            else:
                facts["warnings"].append(
                    "Error occurred while getting information from the BitBucket API. "
                    "(%s)" % err
                )
                return facts
        except URLError as err:
            if str(err.reason).startswith("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]"):
                # TODO(Elliot): Try again using curl? (#19)
                facts["warnings"].append(
                    "I got an SSLv3 handshake error while getting information from the "
                    "BitBucket API, and I don't yet know what to do with that. "
                    "(%s)" % err
                )
                return facts
            else:
                facts["warnings"].append(
                    "Error encountered while getting information from the BitBucket "
                    "API. (%s)" % err
                )
                return facts

        # Get app name.
        if "app_name" not in facts:
            app_name = ""
            robo_print("Getting app name...", LogLevel.VERBOSE)
            if parsed_repo.get("name", "") not in ("", None):
                app_name = parsed_repo["name"]
            if app_name not in ("", None):
                robo_print("App name is: %s" % app_name, LogLevel.VERBOSE, 4)
                facts["app_name"] = app_name

        # Get full name of owner.
        developer = ""
        if "developer" not in facts:
            developer = parsed_repo["owner"]["display_name"]
        if developer not in ("", None):
            robo_print(
                "BitBucket owner full name is: %s" % developer, LogLevel.VERBOSE, 4
            )
            facts["developer"] = developer

        # Get app description.
        if "description" not in facts:
            description = ""
            robo_print("Getting BitBucket description...", LogLevel.VERBOSE)
            if parsed_repo.get("description", "") not in ("", None):
                description = parsed_repo["description"]
            if description not in ("", None):
                robo_print(
                    "BitBucket description is: %s" % description, LogLevel.VERBOSE, 4
                )
                facts["description"] = description
            else:
                facts["warnings"].append("Could not detect BitBucket description.")

        # Get download format of latest release.
        if "download_format" not in facts or "download_url" not in facts:
            download_format = ""
            download_url = ""
            robo_print(
                "Getting information from latest BitBucket release...", LogLevel.VERBOSE
            )
            if "values" in parsed_release:
                # TODO (Elliot): Use find_supported_release() instead of these
                # nested loops. May need to flatten the 'asset' dict first.
                for this_format in ALL_SUPPORTED_FORMATS:
                    for asset in parsed_release["values"]:
                        if download_format not in ("", None):
                            break
                        if asset["links"]["self"]["href"].endswith(this_format):
                            download_format = this_format
                            download_url = asset["links"]["self"]["href"]
                            break
            if download_format not in ("", None):
                robo_print(
                    "BitBucket release download format is: %s" % download_format,
                    LogLevel.VERBOSE,
                    4,
                )
                facts["download_format"] = download_format
            else:
                facts["warnings"].append(
                    "Could not detect BitBucket release download format."
                )
            if download_url not in ("", None):
                robo_print(
                    "BitBucket release download URL is: %s" % download_url,
                    LogLevel.VERBOSE,
                    4,
                )
                facts["download_url"] = download_url
                facts = inspect_download_url(download_url, args, facts)
            else:
                facts["warnings"].append(
                    "Could not detect BitBucket release download URL."
                )

        # Warn user if the BitBucket project is private.
        if parsed_repo.get("is_private", False) is not False:
            facts["warnings"].append(
                'This BitBucket project is marked "private" and recipes '
                "you generate may not work for others."
            )

    else:
        facts["warnings"].append("Could not detect BitBucket repo.")

    return facts


def inspect_disk_image(input_path, args, facts):
    """Process an image

    Gather information required to create a recipe.

    Args:
        input_path: The path or URL that Recipe Robot was asked to use
            to create recipes.
        args: The command line arguments.
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.

    Returns:
        facts dictionary.
    """
    # Only proceed if we haven't inspected this pkg yet.
    if "disk_image" in facts["inspections"]:
        return facts
    else:
        facts["inspections"].append("disk_image")

    # See if we can determine the download URL from the file metadata.
    if "download_url" not in facts:
        get_download_link_from_xattr(input_path, args, facts)

    # Determine whether the dmg has a software license agreement.
    # Inspired by: https://github.com/autopkg/autopkg/blob/master/Code/autopkglib/DmgMounter.py#L74-L98
    dmg_has_sla = False
    cmd = '/usr/bin/hdiutil imageinfo -plist "%s"' % input_path
    exitcode, out, err = get_exitcode_stdout_stderr(cmd)
    if exitcode == 0:
        with open(os.path.join(CACHE_DIR, "dmg_info.plist"), "wb") as dmg_plist:
            dmg_plist.write(out)
        try:
            dmg_info = FoundationPlist.readPlist(
                os.path.join(CACHE_DIR, "dmg_info.plist")
            )
            if dmg_info.get("Properties").get("Software License Agreement") == True:
                dmg_has_sla = True
        except FoundationPlist.NSPropertyListSerializationException:
            pass

    # Mount the dmg and look for an app.
    cmd = '/usr/bin/hdiutil attach -nobrowse -plist "%s"' % input_path
    if dmg_has_sla is True:
        exitcode, out, err = get_exitcode_stdout_stderr(cmd, "Y\n")
    else:
        exitcode, out, err = get_exitcode_stdout_stderr(cmd)
    if exitcode == 0:

        # Confirmed; the download was a disk image. Make a note of that.
        robo_print("Successfully mounted disk image", LogLevel.VERBOSE, 4)
        facts["download_format"] = "dmg"  # most common disk image format

        # If the download filename was ambiguous, change it.
        if not facts.get("download_filename", input_path).endswith(
            SUPPORTED_IMAGE_FORMATS
        ):
            facts["download_filename"] = (
                facts.get("download_filename", input_path) + ".dmg"
            )

        # Clean the output for cases where the dmg has a license
        # agreement.
        out_clean = out[out.find("<?xml") :]

        # Locate and inspect the app.
        with open(os.path.join(CACHE_DIR, "dmg_attach.plist"), "wb") as dmg_plist:
            dmg_plist.write(out_clean)
        try:
            dmg_dict = FoundationPlist.readPlist(
                os.path.join(CACHE_DIR, "dmg_attach.plist")
            )
        except Exception as err:  # pylint: disable=W0703
            raise RoboError(
                "Shoot, I had trouble parsing the output of hdiutil while mounting the "
                "downloaded dmg. Sorry about that.",
                err,
            )
        for entity in dmg_dict["system-entities"]:
            if "mount-point" in entity:
                dmg_mount = entity["mount-point"]
                break
        for this_file in os.listdir(dmg_mount):
            if this_file.lower().endswith(".app"):
                # Copy app to cache folder.
                # TODO(Elliot): What if .app isn't on root of dmg mount? (#26)
                attached_app_path = os.path.join(dmg_mount, this_file)
                cached_app_path = os.path.join(CACHE_DIR, "unpacked", this_file)
                if not os.path.exists(cached_app_path):
                    try:
                        shutil.copytree(attached_app_path, cached_app_path)
                    except shutil.Error:
                        pass
                # Unmount attached volume when done.
                # TODO: Handle unicode characters here. Example:
                # http://download.ap.bittorrent.com/track/stable/endpoint/utmac/os/osx
                cmd = '/usr/bin/hdiutil detach "{}"'.format(dmg_mount)
                exitcode, out, err = get_exitcode_stdout_stderr(cmd)
                facts = inspect_app(cached_app_path, args, facts)
                break
            elif this_file.lower().endswith(tuple([x for x in SUPPORTED_BUNDLE_TYPES])):
                # Copy bundle to cache folder.
                attached_app_path = os.path.join(dmg_mount, this_file)
                cached_app_path = os.path.join(CACHE_DIR, "unpacked", this_file)
                if not os.path.exists(cached_app_path):
                    try:
                        shutil.copytree(attached_app_path, cached_app_path)
                    except shutil.Error:
                        pass
                # Unmount attached volume when done.
                cmd = '/usr/bin/hdiutil detach "%s"' % dmg_mount
                exitcode, out, err = get_exitcode_stdout_stderr(cmd)
                facts = inspect_app(
                    cached_app_path,
                    args,
                    facts,
                    bundle_type=os.path.splitext(this_file)[1].lower().lstrip("."),
                )
                break
            if this_file.lower().endswith(SUPPORTED_INSTALL_FORMATS):
                facts = inspect_pkg(os.path.join(dmg_mount, this_file), args, facts)
                break
    else:
        robo_print(
            "Unable to mount %s. (%s)\n(You can ignore this message if "
            "the upcoming attempt to unzip the downloaded file as an "
            "archive succeeds.)" % (input_path, err),
            LogLevel.DEBUG,
        )

    return facts


def inspect_download_url(input_path, args, facts):
    """Process a direct download URL

    Gather information required to create a recipe.

    Args:
        input_path: The path or URL that Recipe Robot was asked to use
            to create recipes.
        args: The command line arguments.
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.

    Returns:
        facts dictionary.
    """
    # We never skip download URL inspection, even if we've already
    # inspected a download URL during this run. This handles rare
    # situations in which the download URL is in a different format than
    # the Sparkle download.

    # Remove leading and trailing spaces from URL.
    input_path = input_path.strip()

    # Save the download URL to the dictionary of facts.
    robo_print("Download URL is: %s" % input_path, LogLevel.VERBOSE, 4)
    facts["download_url"] = input_path
    facts["is_from_app_store"] = False

    # If download URL is hosted on GitHub or SourceForge, we can gather
    # more information.
    if "github.com" in input_path or "githubusercontent.com" in input_path:
        if "github_repo" not in facts:
            facts = inspect_github_url(input_path, args, facts)
    if "sourceforge.net" in input_path:
        if "sourceforge_id" not in facts:
            facts = inspect_sourceforge_url(input_path, args, facts)

    # Warn if it looks like we're using a version-specific download
    # path, but only if the path was not obtained from a feed of some
    # sort.
    version_match = re.search(r"[\d]+\.[\w]+$", input_path)
    if version_match is not None and (
        "sparkle_feed_url" not in facts["inspections"]
        and "github_url" not in facts["inspections"]
        and "sourceforge_url" not in facts["inspections"]
        and "bitbucket_url" not in facts["inspections"]
    ):
        facts["warnings"].append(
            "Careful, this might be a version-specific URL. Better to give me "
            'a "latest" URL or a Sparkle feed.'
        )

    # Warn if it looks like we're using a temporary CDN URL.
    aws_expire_match = re.search(r"\:\/\/.*Expires\=", input_path)
    if aws_expire_match is not None and (
        "sparkle_feed_url" not in facts["inspections"]
        and "github_url" not in facts["inspections"]
        and "sourceforge_url" not in facts["inspections"]
        and "bitbucket_url" not in facts["inspections"]
    ):
        facts["warnings"].append(
            "This is a CDN-cached URL, and it may expire. Try feeding me a "
            "permanent URL instead."
        )

    # Warn if it looks like we're using an AWS URL with an access key.
    aws_key_match = re.search(r"\:\/\/.*AWSAccessKeyId\=", input_path)
    if aws_key_match is not None and (
        "sparkle_feed_url" not in facts["inspections"]
        and "github_url" not in facts["inspections"]
        and "sourceforge_url" not in facts["inspections"]
        and "bitbucket_url" not in facts["inspections"]
    ):
        facts["warnings"].append("This URL contains an AWSAccessKeyId parameter.")

    # Determine filename from input URL (will be overridden later if a
    # better candidate is found.)
    parsed_url = urlparse(input_path)
    filename = parsed_url.path.split("/")[-1]

    # If the download URL doesn't already end with the parsed filename,
    # it's very likely that URLDownloader needs the filename argument
    # specified.
    facts["specify_filename"] = not input_path.endswith(filename)

    # Check to make sure URL is valid, and switch to HTTPS if possible.
    checked_url = check_url(input_path)
    if checked_url.startswith("http:"):
        facts["warnings"].append(
            "This download URL is not using HTTPS. I recommend contacting the "
            "developer and politely suggesting that they secure their download URL. "
            "(Example: https://twitter.com/homebysix/status/714508127228403712)"
        )

    # Download the file for continued inspection.
    # TODO(Elliot): Maybe something like this is better for downloading
    # big files? https://gist.github.com/gourneau/1430932 (#24)
    robo_print("Downloading file for further inspection...", LogLevel.VERBOSE)

    # Actually download the file.
    try:
        raw_download = urlopen(checked_url)
    except HTTPError as err:
        if err.code == 403:
            # Try again, this time with a user-agent.
            # TODO: Handle requests with cookies (e.g. S3 pre-signed URLs).
            # Example: https://tunnelblick.net/release/Tunnelblick_3.7.8_build_5180.dmg
            # TODO: Try /usr/bin/curl instead?
            try:
                raw_download = useragent_urlopen(checked_url, "Mozilla/5.0")
                facts["user-agent"] = "Mozilla/5.0"
            except Exception as err:  # pylint: disable=W0703
                facts["warnings"].append(
                    "Error encountered during file download. (%s)" % err
                )
                return facts
        if err.code == 404:
            facts["warnings"].append("Download URL not found. (%s)" % err)
            return facts
        else:
            facts["warnings"].append(
                "Error encountered during file download. (%s)" % err
            )
            return facts
    except URLError as err:
        if str(err.reason).startswith("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]"):
            # TODO(Elliot): Try again using curl? (#19)
            facts["warnings"].append(
                "I got an SSLv3 handshake error, and I don't yet know what to "
                "do with that. (%s)" % err
            )
            return facts
        else:
            facts["warnings"].append(
                "Error encountered during file download. (%s)" % err.reason
            )
            return facts
    except CertificateError as err:
        facts["warnings"].append(
            "There seems to be a problem with the developer's SSL certificate. "
            "(%s)" % err
        )
        # TODO: If input path was HTTP, revert to that and try again.
        return facts

    # Warn if we had to add a user agent in order to access URLs above.
    if "user-agent" in facts:
        facts["warnings"].append(
            "I had to use a different user-agent in order to "
            "download this file. If you run the recipes and get a "
            '"Can\'t open URL" error, it means AutoPkg encountered '
            "the same problem."
        )

    # Warn if the content-type is known not to be a downloadable file.
    # TODO: If content-type is HTML, try parsing for download URLs and
    # use URLTextSearcher.
    # TODO: If content-type is XML, see if it's a Sparkle feed or parse for
    # download URLs and use URLTextSearcher.
    if raw_download.info().getheaders("Content-Type"):
        content_type = raw_download.info().getheaders("Content-Type")[0]
        if not content_type.startswith(("binary/", "application/", "file/")):
            facts["warnings"].append(
                "This download's Content-Type ({}) is unusual for a download.".format(
                    content_type
                )
            )

    # Get the actual filename from the server, if it exists.
    if "Content-Disposition" in raw_download.info():
        content_disp = raw_download.info()["Content-Disposition"]
        r_obj = re.search(r"filename=\"(.+)\"\;", content_disp)
        if r_obj is not None:
            filename = r_obj.group(1)

    # If filename was not detected from either the URL or the headers,
    # use a safe default name.
    if filename == "":
        filename = "download"
    facts["download_filename"] = filename

    # Write the downloaded file to the cache folder, showing progress.
    if raw_download.info().getheaders("Content-Length"):
        file_size = int(raw_download.info().getheaders("Content-Length")[0])
    else:
        # File size is unknown, so we can't show progress.
        file_size = 0
    with open(os.path.join(CACHE_DIR, filename), "wb") as download_file:
        file_size_dl = 0
        block_sz = 8192
        while True:
            chunk = raw_download.read(block_sz)
            if not chunk:
                break
            # Write downloaded chunk.
            file_size_dl += len(chunk)
            download_file.write(chunk)
            # Show progress if file size is known.
            if file_size > 0:
                p = float(file_size_dl) / file_size
                stat_fmt = r"    {0:0.0%}" if args.app_mode else r"    {0:.2%}"
                status = stat_fmt.format(p)
                status = status + chr(8) * (len(status) + 1)
                if args.app_mode:
                    # Show progress in 10% increments.
                    if (file_size_dl / block_sz) % (file_size / block_sz / 10) == 0:
                        robo_print(status, LogLevel.VERBOSE)
                else:
                    # Show progress in 0.01% increments.
                    sys.stdout.flush()
                    sys.stdout.write(status)
    robo_print(
        "Downloaded to %s" % os.path.join(CACHE_DIR, filename), LogLevel.VERBOSE, 4
    )

    # Just in case the "download" was actually a Sparkle feed.
    hidden_sparkle = False
    with open(os.path.join(CACHE_DIR, filename), "r") as download_file:
        if download_file.read()[:6] == "<?xml ":
            robo_print("This download is actually a Sparkle feed", LogLevel.VERBOSE, 4)
            hidden_sparkle = True
    if hidden_sparkle is True:
        os.remove(os.path.join(CACHE_DIR, filename))
        facts = inspect_sparkle_feed_url(checked_url, args, facts)
        return facts

    # Try to determine the type of file downloaded. (Overwrites any
    # previous download_type, because the download URL is the most
    # reliable source.)
    download_format = ""
    robo_print("Determining download format...", LogLevel.VERBOSE)
    for this_format in ALL_SUPPORTED_FORMATS:
        if filename.lower().endswith(this_format) or this_format in parsed_url.query:
            download_format = this_format
            facts["download_format"] = this_format
            robo_print("File extension is %s" % this_format, LogLevel.VERBOSE, 4)
            break  # should stop after the first format match

    # If we've already seen the app and the download format, there's no
    # need to unpack the downloaded file.
    if "download_format" in facts and "app" in facts["inspections"]:
        return facts

    robo_print(
        "Download format is unknown, so we're going to try mounting it as a disk image "
        "first, then unarchiving it. This may produce errors, but will hopefully "
        "result in a success.",
        LogLevel.DEBUG,
    )
    robo_print("Opening downloaded file...", LogLevel.VERBOSE)

    # If the file is a webpage (e.g. 404 message), warn the user now.
    with open(os.path.join(CACHE_DIR, filename), "r") as download:
        if "html" in download.readline().lower():
            facts["warnings"].append(
                "There's a good chance that the file failed to download. "
                "Looks like a webpage was downloaded instead."
            )

    # Open the disk image (or test to see whether the download is one).
    if (
        facts.get("download_format", "") == "" or download_format == ""
    ) or download_format in SUPPORTED_IMAGE_FORMATS:
        facts = inspect_disk_image(os.path.join(CACHE_DIR, filename), args, facts)

    # Open the zip archive (or test to see whether the download is one).
    if (
        facts.get("download_format", "") == "" or download_format == ""
    ) or download_format in SUPPORTED_ARCHIVE_FORMATS:
        facts = inspect_archive(os.path.join(CACHE_DIR, filename), args, facts)

    # Inspect the installer (or test to see whether the download is
    # one).
    if download_format in SUPPORTED_INSTALL_FORMATS:

        robo_print("Download format is %s" % download_format, LogLevel.VERBOSE, 4)
        facts["download_format"] = download_format

        # Inspect the package.
        facts = inspect_pkg(os.path.join(CACHE_DIR, filename), args, facts)

    if facts.get("download_format", "") == "":
        facts["warnings"].append(
            "I've investigated pretty thoroughly, and I'm still not sure "
            "what the download format is. This could cause problems later."
        )

    return facts


def github_urlopen(url, github_token):
    """For a given URL, return the urlopen response after adding the GitHub token."""

    req = Request(url)
    if github_token:
        req.add_header("Authorization", "token " + github_token)

    return urlopen(req)


def inspect_github_url(input_path, args, facts):
    """Process a GitHub URL

    Gather information required to create a recipe.

    Args:
        input_path: The path or URL that Recipe Robot was asked to use
            to create recipes.
        args: The command line arguments.
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.

    Returns:
        facts dictionary.
    """
    # Only proceed if we haven't inspected this GitHub URL yet.
    if "github_url" in facts["inspections"]:
        return facts
    else:
        facts["inspections"].append("github_url")

    # Use AutoPkg GitHub token, if file exists.
    # TODO: Also check for GITHUB_TOKEN preference.
    github_token_file = os.path.expanduser("~/.autopkg_gh_token")
    github_token = None
    if os.path.isfile(github_token_file):
        try:
            with open(github_token_file, "r") as tokenfile:
                github_token = tokenfile.read().strip()
        except IOError as err:
            facts["warnings"].append(
                "Couldn't read GitHub token file at {}.".format(github_token_file)
            )

    # Grab the GitHub repo path.
    github_repo = ""
    robo_print("Getting GitHub repo...", LogLevel.VERBOSE)
    # Matches all these examples:
    # - https://github.com/jbtule/cdto/releases/download/2_6_0/cdto_2_6.zip
    # - https://github.com/lindegroup/autopkgr
    # - https://raw.githubusercontent.com/macmule/AutoCasperNBI/master/README.md
    # - https://api.github.com/repos/macmule/AutoCasperNBI
    # - https://hjuutilainen.github.io/munkiadmin/
    parsed_url = urlparse(input_path)
    path = parsed_url.path.split("/")
    path.remove("")
    if "api.github.com" in input_path:
        if "/repos/" not in input_path:
            message = "GitHub API URL specified is not a repository."
            facts["warnings"].append(message)
        else:
            github_repo = path[1] + "/" + path[2]
    elif ".github.io" in input_path:
        github_repo = parsed_url.netloc.split(".")[0] + "/" + path[0]
    else:
        github_repo = path[0] + "/" + path[1]
    if github_repo not in ("", None):
        robo_print("GitHub repo is: %s" % github_repo, LogLevel.VERBOSE, 4)
        facts["github_repo"] = github_repo

        # Leave a hint for people testing Recipe Robot on itself.
        if github_repo == "homebysix/recipe-robot":
            if args.ignore_existing is True:
                facts["reminders"].append(
                    "Congratulations! You've achieved Recipe Robot recursion."
                )
            else:
                facts["warnings"].append(
                    "Try using the --ignore-existing flag if you want me to "
                    "create recipes for myself."
                )

        # Use GitHub API to obtain information about the repo and
        # releases.
        repo_api_url = "https://api.github.com/repos/%s" % github_repo
        releases_api_url = (
            "https://api.github.com/repos/%s/releases/latest" % github_repo
        )
        user_api_url = "https://api.github.com/users/%s" % github_repo.split("/")[0]

        # Download the information from the GitHub API.
        try:
            raw_json_repo = github_urlopen(repo_api_url, github_token).read()
            raw_json_release = github_urlopen(releases_api_url, github_token).read()
            raw_json_user = github_urlopen(user_api_url, github_token).read()
        except HTTPError as err:
            if err.code == 403:
                facts["warnings"].append(
                    "Error occurred while getting information from the GitHub API. If "
                    "you've been creating a lot of recipes quickly, you may have hit "
                    "the rate limit. Give it a few minutes, then try again. (%s)" % err
                )
                return facts
            if err.code == 404:
                facts["warnings"].append("GitHub API URL not found. (%s)" % err)
                return facts
            else:
                facts["warnings"].append(
                    "Error occurred while getting information from the GitHub API. "
                    "(%s)" % err
                )
                # TODO: All of these return facts can just be return, since
                # dicts are passed by reference.
                return facts
        except URLError as err:
            if str(err.reason).startswith("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]"):
                # TODO(Elliot): Try again using curl? (#19)
                facts["warnings"].append(
                    "I got an SSLv3 handshake error while getting information from the "
                    "GitHub API, and I don't yet know what to do with that. (%s)" % err
                )
                return facts
            else:
                facts["warnings"].append(
                    "Error encountered while getting information from the GitHub API. "
                    "(%s)" % err
                )
                return facts

        # Parse the downloaded JSON.
        parsed_repo = json.loads(raw_json_repo)
        parsed_release = json.loads(raw_json_release)
        parsed_user = json.loads(raw_json_user)

        # Get app name.
        if "app_name" not in facts:
            app_name = ""
            robo_print("Getting app name...", LogLevel.VERBOSE)
            if parsed_repo.get("name", None) is not None:
                app_name = parsed_repo["name"]
            if app_name not in ("", None):
                robo_print("App name is: %s" % app_name, LogLevel.VERBOSE, 4)
                facts["app_name"] = app_name

        # Get app description.
        if "description" not in facts:
            description = ""
            robo_print("Getting GitHub description...", LogLevel.VERBOSE)
            if parsed_repo.get("description", None) is not None:
                description = parsed_repo["description"]
            if description not in ("", None):
                robo_print(
                    "GitHub description is: %s" % description, LogLevel.VERBOSE, 4
                )
                facts["description"] = description
            else:
                facts["warnings"].append("No GitHub description provided.")

        # Get download format of latest release.
        if "download_format" not in facts or "download_url" not in facts:
            download_format = ""
            download_url = ""
            robo_print(
                "Getting information from latest GitHub release...", LogLevel.VERBOSE
            )
            if "assets" in parsed_release:
                download_format, download_url = find_supported_release(
                    parsed_release["assets"], "browser_download_url"
                )
                if len(parsed_release["assets"]) > 1:
                    facts["use_asset_regex"] = True
                    robo_print("Multiple formats available.", LogLevel.VERBOSE, 4)
            if download_format not in ("", None):
                robo_print(
                    "GitHub release download format is: %s" % download_format,
                    LogLevel.VERBOSE,
                    4,
                )
                facts["download_format"] = download_format
            else:
                facts["warnings"].append(
                    "Could not detect GitHub release download format."
                )
            if download_url not in ("", None):
                robo_print(
                    "GitHub release download URL is: %s" % download_url,
                    LogLevel.VERBOSE,
                    4,
                )
                facts["download_url"] = download_url
                facts = inspect_download_url(download_url, args, facts)
            else:
                facts["warnings"].append(
                    "Could not detect GitHub release download URL."
                )

        # Get the developer's name from GitHub.
        if "developer" not in facts:
            developer = ""
            robo_print("Getting developer name from GitHub...", LogLevel.VERBOSE)
            if "name" in parsed_user:
                developer = parsed_user["name"]
            if developer not in ("", None):
                robo_print("GitHub developer is: %s" % developer, LogLevel.VERBOSE, 4)
                facts["developer"] = developer
            else:
                facts["warnings"].append("Could not detect GitHub developer.")

        # Warn user if the GitHub project is private.
        if parsed_repo.get("private", False) is not False:
            facts["warnings"].append(
                'This GitHub project is marked "private" and recipes you '
                "generate may not work for others."
            )

        # Warn user if the GitHub project is a fork.
        if parsed_repo.get("fork", False) is not False:
            facts["warnings"].append(
                "This GitHub project is a fork. You may want to try again "
                "with the original repo URL instead."
            )
    else:
        facts["warnings"].append("Could not detect GitHub repo.")

    return facts


def get_apps_from_payload(payload_archive, facts, payload_id=0):
    """Given a path to a package payload, this function expands the payload
    and returns the paths to the apps."""
    payload_apps = []
    payload_dir = os.path.join(CACHE_DIR, "payload%s" % payload_id)
    os.mkdir(payload_dir)
    cmd = '/usr/bin/ditto -x "%s" "%s"' % (payload_archive, payload_dir)
    exitcode, _, err = get_exitcode_stdout_stderr(cmd)
    if exitcode != 0:
        facts["warnings"].append("Ditto failed to expand payload.")
    try:
        os.unlink(payload_archive)
    except OSError as err:
        facts["warnings"].append("Could not remove %s: %s" % (payload_archive, err))

    for dirpath, dirnames, filenames in os.walk(payload_dir):
        for dirname in dirnames:
            if dirname.startswith("."):
                dirnames.remove(dirname)
            elif dirname.endswith(".app") and os.path.isfile(
                os.path.join(dirpath, dirname, "Contents", "Info.plist")
            ):
                payload_apps.append(os.path.join(dirpath, dirname))
    return payload_apps


def get_most_likely_app(app_list):
    """Takes an array of dicts, each with a 'path' key that points to a
    potential app to be evaluated. Uses various criteria to make an educated
    guess about which app is the "real" app, and returns the index of the
    winner. If no winner can be determined, returns None."""

    # Criteria 1: If only one app has a Sparkle feed, choose that one.
    has_sparkle = []
    for index, candidate in enumerate(app_list):
        info_plist = FoundationPlist.readPlist(
            candidate["path"] + "/Contents/Info.plist"
        )
        if (
            "SUFeedURL" in info_plist
            or "SUOriginalFeedURL" in info_plist
            or os.path.exists(
                candidate["path"] + "/Contents/Frameworks/DevMateKit.framework"
            )
        ):
            has_sparkle.append(index)
    if len(has_sparkle) == 1:
        return has_sparkle[0]

    # Criteria 2: If only one app installs into the /Applications folder, choose that one.
    installs_to_apps = []
    for index, candidate in enumerate(app_list):
        head, tail = os.path.split(candidate["path"])
        if head.endswith("/Applications"):
            installs_to_apps.append(index)
    if len(installs_to_apps) == 1:
        return installs_to_apps[0]

    # Criteria 3: Choose largest app by file size.
    largest_size = 0
    largest_index = None
    for index, candidate in enumerate(app_list):
        this_size = 0
        for dirpath, _, filenames in os.walk(candidate["path"]):
            for filename in filenames:
                this_size += os.path.getsize(os.path.join(dirpath, filename))
        if this_size > largest_size:
            largest_size = this_size
            largest_index = index
    return largest_index


def inspect_pkg(input_path, args, facts):
    """Process a package

    Gather information required to create a recipe.

    Args:
        input_path: The path or URL that Recipe Robot was asked to use
            to create recipes.
        args: The command line arguments.
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.

    Returns:
        facts dictionary.
    """
    # Only proceed if we haven't inspected this pkg yet.
    if "pkg" in facts["inspections"]:
        return facts
    else:
        facts["inspections"].append("pkg")

    # See if we can determine the download URL from the file metadata.
    if "download_url" not in facts:
        get_download_link_from_xattr(input_path, args, facts)

    # Check whether package is signed.
    robo_print("Checking whether package is signed...", LogLevel.VERBOSE)
    cmd = '/usr/sbin/pkgutil --check-signature "%s"' % input_path
    exitcode, out, err = get_exitcode_stdout_stderr(cmd)
    if exitcode == 1:
        robo_print("Package is not signed", LogLevel.VERBOSE, 4)
    elif exitcode == 0:
        robo_print("Package is signed", LogLevel.VERBOSE, 4)

        # Get developer name from pkg signature.
        if "developer" not in facts:
            developer = ""
            robo_print("Getting developer from pkg signature...", LogLevel.VERBOSE)
            marker = "    1. Developer ID Installer: "
            for line in out.split("\n"):
                if line.startswith(marker):
                    if " (" in line:
                        line = line.split(" (")[0]
                    developer = line[len(marker) :]
            if developer not in ("", None):
                robo_print("Developer is: %s" % developer, LogLevel.VERBOSE, 4)
                facts["developer"] = developer
            else:
                robo_print("Developer is unknown", LogLevel.VERBOSE, 4)

        # Get code signature verification authority names from pkg
        # signature.
        if len(facts["codesign_authorities"]) == 0:
            codesign_authorities = []
            robo_print("Getting package signature authority names...", LogLevel.VERBOSE)
            for line in out.split("\n"):
                if re.match("^    [\d]\. ", line):
                    codesign_authorities.append(line[7:])
            if len(codesign_authorities) > 0:
                robo_print(
                    "%s authority names recorded" % len(codesign_authorities),
                    LogLevel.VERBOSE,
                    4,
                )
                facts["codesign_authorities"] = codesign_authorities
                facts["codesign_input_filename"] = os.path.basename(input_path)
            else:
                robo_print(
                    "Authority names unknown, treating as unsigned", LogLevel.VERBOSE, 4
                )

    else:
        robo_print(
            "I don't know whether the package is signed - probably not "
            "(pkgutil returned exit code %s)" % exitcode,
            LogLevel.VERBOSE,
            4,
        )

    # Expand the flat package and look for more facts.
    robo_print("Expanding package to look for clues...", LogLevel.VERBOSE)
    expand_path = os.path.join(CACHE_DIR, "expanded")
    if os.path.exists(expand_path):
        shutil.rmtree(expand_path)
    cmd = '/usr/sbin/pkgutil --expand "%s" "%s"' % (input_path, expand_path)
    exitcode, out, err = get_exitcode_stdout_stderr(cmd)
    if exitcode != 0:
        # TODO: Support package bundles here. Example:
        # https://pqrs.org/osx/karabiner/files/KeyRemap4MacBook-7.4.0.pkg.zip
        facts["warnings"].append("Unable to expand package: %s" % input_path)
    else:
        # Locate and inspect the app.
        robo_print(
            "Package expanded to: %s" % os.path.join(CACHE_DIR, "expanded"),
            LogLevel.VERBOSE,
            4,
        )

        payload_id = 0
        payload_apps = []
        found_apps = []
        for dirpath, dirnames, filenames in os.walk(expand_path):
            for dirname in dirnames:
                if dirname.startswith("."):
                    dirnames.remove(dirname)
                elif dirname.endswith(".app") and os.path.isfile(
                    os.path.join(dirpath, dirname, "Contents", "Info.plist")
                ):
                    found_apps.append(
                        {
                            "path": os.path.join(dirpath, dirname),
                            "pkg_filename": os.path.basename(input_path),
                        }
                    )  # should be rare
            for filename in filenames:
                if filename.endswith(".pkg"):  # flat packages
                    facts["warnings"].append(
                        "Yo dawg, I found a flat package inside this flat package!"
                    )
                    # TODO (Elliot): Recursion! Any chance for a loop here?
                    facts = inspect_pkg(os.path.join(dirpath, filename), args, facts)
                elif filename == "PackageInfo":
                    robo_print(
                        "Trying to get bundle identifier from PackageInfo file...",
                        LogLevel.VERBOSE,
                    )
                    pkginfo_file = open(os.path.join(dirpath, filename), "r")
                    pkginfo_parsed = parse(pkginfo_file)
                    bundle_id = ""
                    if "bundle_id" not in facts:
                        bundle_id = pkginfo_parsed.getroot().attrib["identifier"]
                    if bundle_id not in ("", None):
                        robo_print(
                            "Bundle identifier (tentative): %s" % bundle_id,
                            LogLevel.VERBOSE,
                            4,
                        )
                        facts["bundle_id"] = bundle_id
                    else:
                        robo_print(
                            "No bundle identifier in this PackageInfo.",
                            LogLevel.VERBOSE,
                            4,
                        )
                elif filename.lower() == "payload":
                    payload_apps = get_apps_from_payload(
                        os.path.join(dirpath, filename), facts, payload_id
                    )
                    for app in payload_apps:
                        found_apps.append(
                            {
                                "path": app,
                                "pkg_filename": os.path.join(dirpath, filename).split(
                                    "/"
                                )[-2],
                            }
                        )
                    payload_id += 1

        # Add apps found to blocking applications, unless
        # otherwise specified.
        non_blocking_apps = (
            "autoupdate.app",
            "install.app",
            "installer.app",
            "uninstall.app",
            "uninstaller.app",
        )
        for app in [os.path.basename(x["path"]) for x in found_apps]:
            if app not in facts["blocking_applications"]:
                if app.lower() in non_blocking_apps:
                    robo_print("Found application: %s" % app, LogLevel.VERBOSE, 4)
                else:
                    robo_print(
                        "Added blocking application: %s" % app, LogLevel.VERBOSE, 4
                    )
                    facts["blocking_applications"].append(app)

        # Determine which app, if any, to process further.
        payload_dir = os.path.join(CACHE_DIR, "payload")
        if len(found_apps) == 0:
            facts["warnings"].append("No apps found in payload.")
        elif len(found_apps) == 1:
            robo_print(
                "Using app: %s" % os.path.basename(found_apps[0]["path"]),
                LogLevel.VERBOSE,
                4,
            )
            robo_print(
                "In container package: %s" % found_apps[0]["pkg_filename"],
                LogLevel.VERBOSE,
                4,
            )
            relpath = os.path.relpath(found_apps[0]["path"], CACHE_DIR).split("/")[1:]
            facts["app_relpath_from_payload"] = "/".join(relpath)
            facts["pkg_filename"] = found_apps[0]["pkg_filename"]
            facts = inspect_app(found_apps[0]["path"], args, facts)
        elif len(found_apps) > 1:
            facts["warnings"].append(
                "Multiple apps found in payload. I'll do my best to figure "
                "out which one to use."
            )
            app_index = get_most_likely_app(found_apps)
            robo_print(
                "Using app: %s" % os.path.basename(found_apps[app_index]["path"]),
                LogLevel.VERBOSE,
                4,
            )
            robo_print(
                "In container package: %s" % found_apps[app_index]["pkg_filename"],
                LogLevel.VERBOSE,
                4,
            )
            relpath = os.path.relpath(found_apps[app_index]["path"], CACHE_DIR).split(
                "/"
            )[1:]
            facts["app_relpath_from_payload"] = "/".join(relpath)
            facts["pkg_filename"] = found_apps[app_index]["pkg_filename"]
            facts = inspect_app(found_apps[app_index]["path"], args, facts)

    return facts


def inspect_sourceforge_url(input_path, args, facts):
    """Process a SourceForge URL

    Gather information required to create a recipe.

    Args:
        input_path: The path or URL that Recipe Robot was asked to use
            to create recipes.
        args: The command line arguments.
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.

    Returns:
        facts dictionary.
    """
    # Only proceed if we haven't inspected this SourceForge URL yet.
    if "sourceforge_url" in facts["inspections"]:
        return facts
    else:
        facts["inspections"].append("sourceforge_url")

    # Determine the name of the SourceForge project.
    proj_name = ""
    if "/projects/" in input_path:
        # Example: https://sourceforge.net/projects/adium/?source=recommended
        # Example: https://sourceforge.net/projects/grandperspectiv
        # Example: https://sourceforge.net/projects/grandperspectiv/
        marker = "/projects/"
        proj_str = input_path[input_path.find(marker) + len(marker) :]
        if proj_str.find("/") > 0:
            proj_name = proj_str[: proj_str.find("/")]
        else:
            proj_name = proj_str
    elif "/p/" in input_path:
        # Example: https://sourceforge.net/p/grandperspectiv/wiki/Home/
        marker = "/p/"
        proj_str = input_path[input_path.find(marker) + len(marker) :]
        if proj_str.find("/") > 0:
            proj_name = proj_str[: proj_str.find("/")]
        else:
            proj_name = proj_str
    elif ".sourceforge.net" in input_path:
        # Example: http://grandperspectiv.sourceforge.net/
        # Example: http://grandperspectiv.sourceforge.net/screenshots.html
        marker = ".sourceforge.net"
        proj_str = input_path.lstrip("http://")
        proj_name = proj_str[: proj_str.find(marker)]
    else:
        facts["warnings"].append("Unable to parse SourceForge URL.")
    if proj_name not in ("", None):

        # Use SourceForge API to obtain project information.
        project_api_url = "https://sourceforge.net/rest/p/" + proj_name
        try:
            raw_json = urlopen(project_api_url).read()
        except HTTPError as err:
            if err.code == 403:
                facts["warnings"].append(
                    "Error occurred while getting information from the SourceForge "
                    "API. If you've been creating a lot of recipes quickly, you may "
                    "have hit the rate limit. Give it a few minutes, then try again. "
                    "(%s)" % err
                )
                return facts
            if err.code == 404:
                facts["warnings"].append("SourceForge API URL not found. (%s)" % err)
                return facts
            else:
                facts["warnings"].append(
                    "Error occurred while getting information from the SourceForge "
                    "API. (%s)" % err
                )
                return facts
        except URLError as err:
            if str(err.reason).startswith("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]"):
                # TODO(Elliot): Try again using curl? (#19)
                facts["warnings"].append(
                    "I got an SSLv3 handshake error while getting information "
                    "from the SourceForge API, and I don't yet know what to "
                    "do with that. (%s)" % err
                )
                return facts
            else:
                facts["warnings"].append(
                    "Error encountered while getting information from the "
                    "SourceForge API. (%s)" % err
                )
                return facts
        parsed_json = json.loads(raw_json)

        # Get app name.
        if "app_name" not in facts:
            if "shortname" in parsed_json or "name" in parsed_json:
                # Record the shortname, if shortname isn't blank.
                if parsed_json["shortname"] not in ("", None):
                    app_name = parsed_json["shortname"]
                # Overwrite shortname with name, if name isn't blank.
                if parsed_json["name"] not in ("", None):
                    app_name = parsed_json["name"]
            if app_name not in ("", None):
                robo_print("App name is: %s" % app_name, LogLevel.VERBOSE, 4)
                facts["app_name"] = app_name

        # Determine project ID.
        proj_id = ""
        robo_print("Getting SourceForge project ID...", LogLevel.VERBOSE)
        for this_dict in parsed_json["tools"]:
            if "sourceforge_group_id" in this_dict:
                proj_id = this_dict["sourceforge_group_id"]
        if proj_id not in ("", None):
            robo_print("SourceForge project ID is: %s" % proj_id, LogLevel.VERBOSE, 4)
            facts["sourceforge_id"] = proj_id
        else:
            facts["warnings"].append("Could not detect SourceForge project ID.")

        # Get project description.
        if "description" not in facts:
            description = ""
            robo_print("Getting SourceForge description...", LogLevel.VERBOSE)
            if "summary" in parsed_json:
                if parsed_json["summary"] not in ("", None):
                    description = parsed_json["summary"]
                elif parsed_json["short_description"] not in ("", None):
                    description = parsed_json["short_description"]
            if description not in ("", None):
                robo_print(
                    "SourceForge description is: %s" % description, LogLevel.VERBOSE, 4
                )
                facts["description"] = description
            else:
                facts["warnings"].append("Could not detect SourceForge description.")

        # Get download format of latest release.
        if "download_url" not in facts:

            # Download the RSS feed and parse it.
            # Example: https://sourceforge.net/projects/grandperspectiv/rss
            # Example: https://sourceforge.net/projects/cord/rss
            files_rss = "https://sourceforge.net/projects/%s/rss" % proj_name
            try:
                raw_xml = urlopen(files_rss)
            except Exception as err:  # pylint: disable=W0703
                facts["warnings"].append(
                    "Error occurred while inspecting SourceForge RSS feed: %s" % err
                )
            doc = parse(raw_xml)

            # Get the latest download URL.
            download_url = ""
            robo_print(
                "Determining download URL from SourceForge RSS feed...",
                LogLevel.VERBOSE,
            )
            for item in doc.iterfind("channel/item"):
                # TODO(Elliot): The extra-info tag is not a reliable
                # indicator of which item should actually be downloaded.
                # (#21) Example:
                # https://sourceforge.net/projects/grandperspectiv/rss
                search = "{https://sourceforge.net/api/files.rdf#}extra-info"
                if item.find(search).text.startswith("data"):
                    download_url = item.find("link").text.rstrip("/download")
                    break
            if download_url not in ("", None):
                facts = inspect_download_url(download_url, args, facts)
            else:
                facts["warnings"].append(
                    "Could not detect SourceForge latest release download_url."
                )

        # Warn user if the SourceForge project is private.
        if "private" in parsed_json:
            if parsed_json["private"] is True:
                facts["warnings"].append(
                    'This SourceForge project is marked "private" and '
                    "recipes you generate may not work for others."
                )

    else:
        facts["warnings"].append("Could not detect SourceForge project name.")

    return facts


def inspect_sparkle_feed_url(input_path, args, facts):
    """Process a Sparkle feed URL

    Gather information required to create a recipe.

    Args:
        input_path: The path or URL that Recipe Robot was asked to use
            to create recipes.
        args: The command line arguments.
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.

    Returns:
        facts dictionary.
    """
    # Only proceed if we haven't inspected this Sparkle feed yet.
    if "sparkle_feed_url" in facts["inspections"]:
        return facts
    else:
        facts["inspections"].append("sparkle_feed_url")

    # Save the Sparkle feed URL to the dictionary of facts.
    robo_print("Sparkle feed is: %s" % input_path, LogLevel.VERBOSE, 4)
    facts["sparkle_feed"] = input_path

    # Check to make sure URL is valid, and switch to HTTPS if possible.
    checked_url = check_url(input_path)
    if checked_url.startswith("http:"):
        facts["warnings"].append(
            "This Sparkle feed is not using HTTPS. I recommend contacting "
            "the developer and politely suggesting that they secure "
            "their Sparkle feed. (Example: "
            "https://twitter.com/homebysix/status/714508127228403712)"
        )

    # Download the Sparkle feed.
    try:
        raw_xml = urlopen(checked_url)
    except HTTPError as err:
        if err.code == 403:
            # Try again, this time with a user-agent.
            # TODO: Handle requests with cookies (e.g. S3 pre-signed URLs).
            # Example: https://tunnelblick.net/release/Tunnelblick_3.7.8_build_5180.dmg
            # TODO: Try /usr/bin/curl instead?
            try:
                raw_xml = useragent_urlopen(checked_url, "Mozilla/5.0")
                facts["user-agent"] = "Mozilla/5.0"
            except Exception as err:  # pylint: disable=W0703
                facts["warnings"].append(
                    "Error occurred while downloading Sparkle feed (%s)" % err
                )
                # Remove Sparkle feed if it's not usable.
                facts.pop("sparkle_feed", None)
                return facts
        if err.code == 404:
            facts["warnings"].append("Sparkle feed not found. (%s)" % err)
            facts.pop("sparkle_feed", None)
            return facts
        else:
            facts["warnings"].append(
                "Error occurred while getting information from the Sparkle "
                "feed. (%s)" % err
            )
            facts.pop("sparkle_feed", None)
            return facts
    except URLError as err:
        if str(err.reason).startswith("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]"):
            # TODO(Elliot): Try again using curl? (#19)
            facts["warnings"].append(
                "I got an SSLv3 handshake error while getting information "
                "from the Sparkle feed, and I don't yet know what to do with "
                "that.  (%s)" % err
            )
            facts.pop("sparkle_feed", None)
            return facts
        else:
            facts["warnings"].append(
                "Error encountered while getting information from the "
                "Sparkle feed. (%s)" % err
            )
            facts.pop("sparkle_feed", None)
            return facts
    except CertificateError as err:
        facts["warnings"].append(
            "There seems to be a problem with the developer's SSL "
            "certificate. (%s)" % err
        )
        # TODO: If input path was HTTP, revert to that and try again.
        return facts

    # Warn if we had to add a user agent in order to access URLs above.
    if "user-agent" in facts:
        facts["warnings"].append(
            "I had to use a different user-agent in order to read "
            "this Sparkle feed. If you run the recipes and get a "
            '"Can\'t open URL" error, it means AutoPkg encountered '
            "the same problem."
        )

    # Parse the Sparkle feed.
    xmlns = "http://www.andymatuschak.org/xml-namespaces/sparkle"
    try:
        doc = parse(raw_xml)
    except ParseError as err:
        facts["warnings"].append("Error occurred while parsing Sparkle feed (%s)" % err)
        facts.pop("sparkle_feed", None)
        return facts

    # Determine whether the Sparkle feed provides a version number.
    sparkle_provides_version = False
    latest_version = "0"
    latest_url = ""
    robo_print("Getting information from Sparkle feed...", LogLevel.VERBOSE)
    for item in doc.iterfind("channel/item/enclosure"):
        latest_url = item.attrib.get("url")
        for vers_tag in ("shortVersionString", "version"):
            encl_vers = item.get("{%s}%s" % (xmlns, vers_tag))
            if encl_vers not in (None, ""):
                sparkle_provides_version = True
                if LooseVersion(encl_vers) > LooseVersion(latest_version):
                    latest_version = encl_vers

    if sparkle_provides_version is True:
        robo_print("The Sparkle feed provides a version number", LogLevel.VERBOSE, 4)
    else:
        robo_print(
            "The Sparkle feed does not provide a version number", LogLevel.VERBOSE, 4
        )
    facts["sparkle_provides_version"] = sparkle_provides_version
    if latest_version not in ("", None):
        robo_print("The latest version is %s" % latest_version, LogLevel.VERBOSE, 4)
    if latest_url not in ("", None):
        facts = inspect_download_url(latest_url, args, facts)

    # If Sparkle feed is hosted on GitHub or SourceForge, we can gather
    # more information.
    if "github.com" in checked_url or "githubusercontent.com" in checked_url:
        if "github_repo" not in facts:
            facts = inspect_github_url(checked_url, args, facts)
    if "sourceforge.net" in checked_url:
        if "sourceforge_id" not in facts:
            facts = inspect_sourceforge_url(checked_url, args, facts)

    return facts
