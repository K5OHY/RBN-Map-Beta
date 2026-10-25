"""The app: page setup and navigation. Each page lives in views/."""
import streamlit as st

from ui_common import CSS, github_url
from views.compare_page import compare_page
from views.conditions_view import conditions_page
from views.help_view import help_page
from views.spots_view import spots_page


def main():
    st.set_page_config(layout="wide", page_title="RBN Signal Mapper", page_icon="\U0001F4E1")
    st.markdown(CSS, unsafe_allow_html=True)
    pages = [st.Page(spots_page, title="My spots", icon="\U0001F4E1", url_path="spots", default=True),
             st.Page(compare_page, title="Compare antennas", icon="⚖️", url_path="compare"),
             st.Page(conditions_page, title="Band conditions", icon="\U0001F310", url_path="conditions"),
             st.Page(help_page, title="Help and README", icon="\u2753", url_path="help")]
    st.navigation(pages).run()
    st.sidebar.divider()
    st.sidebar.caption(f"Built by Tim, K5OHY \u00b7 [README and code on GitHub]({github_url()}#readme) \u00b7 long-test method by "
                       "[Dale, N3HXZ](https://www.n3hxz.net/)")


if __name__ == "__main__":
    main()
