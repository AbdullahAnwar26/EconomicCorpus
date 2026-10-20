from lxml import etree


def extract_xml(path):
    parser = etree.XMLParser(
        recover=True,
        huge_tree=True
    )

    tree = etree.parse(str(path), parser)

    return etree.tostring(
        tree,
        encoding="unicode"
    )
