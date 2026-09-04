import urllib.request
import urllib.parse
import re
import fnmatch

class Bing:
    def __init__(self, query, limit, adult="off", timeout=60, filter='', excludeSites=[], verbose=False):
        self.query = query
        self.adult = adult
        self.filter = filter
        self.verbose = verbose
        self.seen = set()
        self.excludeSites = excludeSites

        assert type(limit) == int, "limit must be integer"
        self.limit = limit
        assert type(timeout) == int, "timeout must be integer"
        self.timeout = timeout

        self.page_counter = 0
        self.headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) ' \
      'AppleWebKit/537.11 (KHTML, like Gecko) ' \
      'Chrome/23.0.1271.64 Safari/537.11',
      'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.3',
      'Accept-Encoding': 'none',
      'Accept-Language': 'en-US,en;q=0.8',
      'Connection': 'keep-alive'}

    def get_filter(self, shorthand):
        filters = {
            "line": "+filterui:photo-linedrawing",
            "linedrawing": "+filterui:photo-linedrawing",
            "photo": "+filterui:photo-photo",
            "clipart": "+filterui:photo-clipart",
            "gif": "+filterui:photo-animatedgif",
            "animatedgif": "+filterui:photo-animatedgif",
            "transparent": "+filterui:photo-transparent"
        }
        return filters.get(shorthand, "")

    def get_image_links(self, function = None):
        image_links = []
        while len(image_links) < self.limit:
            if self.verbose:
                print(f'\n\n[!!] Indexing page: {self.page_counter + 1}\n')
            
            request_url = f'https://www.bing.com/images/async?q={urllib.parse.quote_plus(self.query)}'
            request_url += f'&first={self.page_counter}&count={self.limit}&adlt={self.adult}&qft={self.get_filter(self.filter)}'
            
            request = urllib.request.Request(request_url, None, headers=self.headers)
            response = urllib.request.urlopen(request)
            html = response.read().decode('utf8')
            
            if not html:
                print("[%] No more images are available")
                break
            
            links = re.findall('murl&quot;:&quot;(.*?)&quot;', html)
            links = [link.replace(" ", "%20") for link in links]
            
            if self.verbose:
                print(f'[%] Indexed {len(links)} Images on Page {self.page_counter + 1}.')
                print("\n===============================================\n")

            for link in links:
                parsed_url = urllib.parse.urlparse(link)
                domain = parsed_url.netloc.lower()

                if any(fnmatch.fnmatch(domain, pattern) for pattern in self.excludeSites):
                    continue  # Bỏ qua link này
                if len(image_links) < self.limit and link not in self.seen:
                    self.seen.add(link)
                    image_links.append(link)
                    if function:
                        try:
                            function(link)
                        except Exception as e:
                            print(e)

            self.page_counter += 1

        if self.verbose:
            print(f"\n\n[%] Done. Collected {len(image_links)} image links.")
        return image_links
