### Install

**For Fresh Install Only – If you’ve already installed before, I assume you know how to update/upgrade an app…**

**Method 1** - Add Crystal Heeler's repo via quick link

[![Add Crystal Heeler's Widget Wonderland to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fcrystalheeler%2Fcrystalheeler)

I honestly don’t know how this works, but offering it for those of you who use it. I think once you add it, you’ll have to go to the App Store as in step 7 below, and go from there…

**Method 2** – Add Crystal Heeler’s repo manually

1. Copy this URL: https://github.com/crystalheeler/crystalheeler
2. Go to Settings -> Apps -> Install (bottom right)
3. Hit the 3-dot elipse in the upper right and select Repositories
4. Click +Add (bottom right)
5. Paste the URL
6. Confirm “Crystal Heeler’s Widget Wonderland” appears in the Repositories list immediately after adding.
7. Hit back once, so that you are now at App Store
8. Scroll down till you see Crystal Heeler’s, then click on the AnyCam card
9. Click Install

**Method 3** – Download zip, drag and drop onto your HA system

1. Download the zip file below
2. Move it to `/addons` on your HA system
3. Unzip
4. In HA, go to Settings → Apps → Install App (bottom right)
5. You should see the AnyCam card at the very top under Local Apps
6. If you don’t, go to the elipse in the upper right and click Check for Updates

An App store install is a separate add-on from a copy in `/addons`. Uninstall the `/addons` copy first, or you will have 2 conflicting instances of AnyCam running on your system at the same time…
