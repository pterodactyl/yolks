# Yolks

A curated collection of core images that can be used with Pterodactyl's Egg system. Each image is rebuilt
periodically to ensure dependencies are always up-to-date.

Images are hosted on `ghcr.io` and exist under the `games`, `installers`, and `yolks` spaces. The following logic
is used when determining which space an image will live under:

* `oses` — base images containing core packages to get you started.
* `games` — anything within the `games` folder in the repository. These are images built for running a specific game
or type of game.
* `installers` — anything living within the `installers` directory. These images are used by install scripts for different
Eggs within Pterodactyl, not for actually running a game server. These images are only designed to reduce installation time
and network usage by pre-installing common installation dependencies such as `curl` and `wget`.
* `yolks` — these are more generic images that allow different types of games or scripts to run. They're generally just
a specific version of software and allow different Eggs within Pterodactyl to switch out the underlying implementation. An
example of this would be something like Java or Python which are used for running bots, Minecraft servers, etc.

All of these images are available for `linux/amd64` and `linux/arm64` versions, unless otherwise specified, to use
these images on an arm64 system, no modification to them or the tag is needed, they should just work.

## Contributing

When adding a new version to an existing image, such as `java v42`, you'd add it within a child folder of `java`, so
`java/42/Dockerfile` for example. Please also update the correct `.github/workflows` file to ensure that this new version
is tagged correctly.

## Available Images

* [`base oses`](/oses)
  * [`alpine`](/oses/alpine)
    * `ghcr.io/pterodactyl/yolks:alpine`
  * [`debian`](/oses/debian)
    * `ghcr.io/pterodactyl/yolks:debian`

* [`games`](/games)
  * [`rust`](/games/rust)
    * `ghcr.io/pterodactyl/games:rust`
  * [`source`](/games/source)
    * `ghcr.io/pterodactyl/games:source`
  * [`hytale`](/games/hytale)
    * `ghcr.io/pterodactyl/games:hytale`
  * [`conan_exiles`](/games/conan_exiles)
    * `ghcr.io/pterodactyl/games:conan_exiles`

* [`golang`](/go)
  * [`go1.14`](/go/1.14)
    * `ghcr.io/pterodactyl/yolks:go_1.14`
  * [`go1.15`](/go/1.15)
    * `ghcr.io/pterodactyl/yolks:go_1.15`
  * [`go1.16`](/go/1.16)
    * `ghcr.io/pterodactyl/yolks:go_1.16`
  * [`go1.17`](/go/1.17)
    * `ghcr.io/pterodactyl/yolks:go_1.17`
  * [`go1.18`](/go/1.18)
    * `ghcr.io/pterodactyl/yolks:go_1.18`
  * [`go1.19`](/go/1.19)
    * `ghcr.io/pterodactyl/yolks:go_1.19`
  * [`go1.20`](/go/1.20)
    * `ghcr.io/pterodactyl/yolks:go_1.20`
  * [`go1.21`](/go/1.21)
    * `ghcr.io/pterodactyl/yolks:go_1.21`
  * [`go1.22`](/go/1.22)
    * `ghcr.io/pterodactyl/yolks:go_1.22`
  * [`go1.23`](/go/1.23)
    * `ghcr.io/pterodactyl/yolks:go_1.23`
  * [`go1.24`](/go/1.24)
    * `ghcr.io/pterodactyl/yolks:go_1.24`
  * [`go1.25`](/go/1.25)
    * `ghcr.io/pterodactyl/yolks:go_1.25`
  * [`go1.26`](/go/1.26)
    * `ghcr.io/pterodactyl/yolks:go_1.26`
    * `ghcr.io/pterodactyl/yolks:go_latest`

* [`java`](/java)
  * [`java8`](/java/8)
    * `ghcr.io/pterodactyl/yolks:java_8`
  * [`java8 - OpenJ9`](/java/8j9)
    * `ghcr.io/pterodactyl/yolks:java_8j9`
  * [`java11`](/java/11)
    * `ghcr.io/pterodactyl/yolks:java_11`
  * [`java11 - OpenJ9`](/java/11j9)
    * `ghcr.io/pterodactyl/yolks:java_11j9`
  * [`java16`](/java/16)
    * `ghcr.io/pterodactyl/yolks:java_16`
  * [`java16 - OpenJ9`](/java/16j9)
    * `ghcr.io/pterodactyl/yolks:java_16j9`
  * [`java17`](/java/17)
    * `ghcr.io/pterodactyl/yolks:java_17`
  * [`java17 - OpenJ9`](/java/17j9)
    * `ghcr.io/pterodactyl/yolks:java_17j9`
  * [`java18`](/java/18)
    * `ghcr.io/pterodactyl/yolks:java_18`
  * [`java18 - OpenJ9`](/java/18j9)
    * `ghcr.io/pterodactyl/yolks:java_18j9`
  * [`java19`](/java/19)
    * `ghcr.io/pterodactyl/yolks:java_19`
  * [`java19 - OpenJ9`](/java/19j9)
    * `ghcr.io/pterodactyl/yolks:java_19j9`
  * [`java21`](/java/21)
    * `ghcr.io/pterodactyl/yolks:java_21`
  * [`java22`](/java/22)
    * `ghcr.io/pterodactyl/yolks:java_22`
  * [`java23`](/java/23)
    * `ghcr.io/pterodactyl/yolks:java_23`
  * [`java24`](/java/24)
    * `ghcr.io/pterodactyl/yolks:java_24`
  * [`java25`](/java/25)
    * `ghcr.io/pterodactyl/yolks:java_25`

* [`nodejs`](/nodejs)
  * [`node12`](/nodejs/12)
    * `ghcr.io/pterodactyl/yolks:nodejs_12`
  * [`node14`](/nodejs/14)
    * `ghcr.io/pterodactyl/yolks:nodejs_14`
  * [`node15`](/nodejs/15)
    * `ghcr.io/pterodactyl/yolks:nodejs_15`
  * [`node16`](/nodejs/16)
    * `ghcr.io/pterodactyl/yolks:nodejs_16`
  * [`node17`](/nodejs/17)
    * `ghcr.io/pterodactyl/yolks:nodejs_17`
  * [`node18`](/nodejs/18)
    * `ghcr.io/pterodactyl/yolks:nodejs_18`
  * [`node20`](/nodejs/18)
    * `ghcr.io/pterodactyl/yolks:nodejs_20`

* [`python`](/python)
  * [`python3.7`](/python/3.7)
    * `ghcr.io/pterodactyl/yolks:python_3.7`
  * [`python3.8`](/python/3.8)
    * `ghcr.io/pterodactyl/yolks:python_3.8`
  * [`python3.9`](/python/3.9)
    * `ghcr.io/pterodactyl/yolks:python_3.9`
  * [`python3.10`](/python/3.10)
    * `ghcr.io/pterodactyl/yolks:python_3.10`

### Installation Images

* [`alpine-install`](/installers/alpine)
  * `ghcr.io/pterodactyl/installers:alpine`

* [`debian-install`](/installers/debian)
  * `ghcr.io/pterodactyl/installers:debian`
