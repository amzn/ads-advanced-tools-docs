# Installation

The Amazon Events API solution consists of three Google Tag Manager templates that need to be imported into your GTM server container. The import order matters — variable templates should be imported before the tag template so that references resolve correctly.

## Importing the Templates

In your Google Tag Manager server container, go to **Templates**:

1. Click **"New"** to create a new template. You will import one tag template and two variable templates.

   ![installation 1](../../images/installation%201.png)

   ![installation 2](../../images/installation%202.png)

2. Click **"Import"** and select each `.tpl` file from the repository shared by your Amazon representative:

   ![installation 3](../../images/installation%203.png)

3. Import the templates in this order:
   - `Amazon_CAPI_Auth_Variable.tpl` first
   - `Amazon_CAPI_Timestamp_Variable.tpl` second
   - `Amazon_Events_API_Tag_Template.tpl` last

4. Save each template after importing.

---

With the templates imported, move on to [Configuration](configuration.md) to set up authentication, timestamps, and the main tag.
