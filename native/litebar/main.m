/*
 * local-ai-monitor-litebar — always-on NSStatusItem (AppKit only, no SwiftUI).
 *
 * Native menu bar fallback:
 *   chip · sessions (GB) · reclaim idle · resource interrupt · quit
 * Reads live.min.json (C daemon) or live.json fallback. Never runs ps.
 *
 * Build: make -C native litebar
 * Budget: measure RSS after load; goal ≪ glass menubar, same job.
 */

#import <Cocoa/Cocoa.h>
#import <Foundation/Foundation.h>
#include <signal.h>

static NSString *DefaultLivePath(void) {
  NSString *env = [[[NSProcessInfo processInfo] environment] objectForKey:@"LOCAL_AI_MONITOR_LIVE"];
  if (env.length)
    return [env stringByExpandingTildeInPath];
  NSString *st = [[[NSProcessInfo processInfo] environment] objectForKey:@"LOCAL_AI_MONITOR_STATE"];
  if (st.length) {
    NSString *min = [[st stringByExpandingTildeInPath] stringByAppendingPathComponent:@"live.min.json"];
    if ([[NSFileManager defaultManager] isReadableFileAtPath:min])
      return min;
    return [[st stringByExpandingTildeInPath] stringByAppendingPathComponent:@"live.json"];
  }
  NSString *home = NSHomeDirectory();
  NSString *min = [home stringByAppendingPathComponent:@".local/state/local-ai-monitor/live.min.json"];
  if ([[NSFileManager defaultManager] isReadableFileAtPath:min])
    return min;
  return [home stringByAppendingPathComponent:@".local/state/local-ai-monitor/live.json"];
}

static double GbFromRssKb(id v) {
  double kb = [v respondsToSelector:@selector(doubleValue)] ? [v doubleValue] : 0;
  return kb / (1024.0 * 1024.0);
}

@interface LiteBar : NSObject <NSApplicationDelegate>
@property (strong) NSStatusItem *item;
@property (strong) NSTimer *timer;
@property (copy) NSString *livePath;
@property (strong) NSDictionary *snap;
@property (strong) NSMutableArray<NSDictionary *> *sessionRows; // for reclaim-idle / end
@end

@implementation LiteBar

- (void)applicationDidFinishLaunching:(NSNotification *)n {
  (void)n;
  self.livePath = DefaultLivePath();
  self.sessionRows = [NSMutableArray array];
  self.item = [[NSStatusBar systemStatusBar] statusItemWithLength:NSVariableStatusItemLength];
  self.item.button.title = @"AI";
  self.item.button.toolTip = @"Local AI Monitor (lite)";
  [self rebuildMenu];
  [self reload];
  self.timer = [NSTimer scheduledTimerWithTimeInterval:2.0
                                                target:self
                                              selector:@selector(reload)
                                              userInfo:nil
                                               repeats:YES];
  [[NSRunLoop mainRunLoop] addTimer:self.timer forMode:NSRunLoopCommonModes];
}

- (NSDictionary *)loadSnap {
  NSData *data = [NSData dataWithContentsOfFile:self.livePath];
  if (!data)
    return nil;
  id obj = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
  return [obj isKindOfClass:[NSDictionary class]] ? obj : nil;
}

- (void)reload {
  self.snap = [self loadSnap];
  if (!self.snap) {
    self.item.button.title = @"AI · —";
    // Rebuild only if menu is open (cheap when closed).
    if (self.item.menu.highlightedItem != nil || self.item.menu.numberOfItems == 0)
      [self rebuildMenu];
    return;
  }
  NSDictionary *res = self.snap[@"resource"];
  NSDictionary *tot = self.snap[@"totals"];
  NSString *title = @"AI";
  if ([res isKindOfClass:[NSDictionary class]] && [res[@"show"] boolValue] && res[@"chip"]) {
    title = [NSString stringWithFormat:@"%@", res[@"chip"]];
  } else if ([tot isKindOfClass:[NSDictionary class]]) {
    double cpu = [tot[@"cpu_pct"] doubleValue];
    double gb = GbFromRssKb(tot[@"rss_kb"]);
    double headMb = -1;
    if ([res isKindOfClass:[NSDictionary class]] && res[@"headroom_mb"] != nil &&
        res[@"headroom_mb"] != [NSNull null])
      headMb = [res[@"headroom_mb"] doubleValue];
    if (headMb >= 0 && headMb < 800)
      title = [NSString stringWithFormat:@"AI · %.1fG head", headMb / 1024.0];
    else if (gb >= 0.05)
      title = [NSString stringWithFormat:@"AI · %.0f%% · %.2fG", cpu, gb];
    else
      title = [NSString stringWithFormat:@"AI · %.0f%%", cpu];
  }
  self.item.button.title = title;
  // Full menu rebuild only while open — not every poll tick (BUDGET lazy build).
  if (self.item.menu != nil && self.item.menu.highlightedItem != nil)
    [self rebuildMenu];
  else if (self.item.menu == nil)
    [self rebuildMenu];
}

- (void)rebuildMenu {
  NSMenu *m = [NSMenu new];
  m.autoenablesItems = NO;

  if (!self.snap) {
    [m addItemWithTitle:@"Monitor offline — start local-ai-monitord" action:NULL keyEquivalent:@""];
    [self addFooter:m];
    self.item.menu = m;
    return;
  }

  NSDictionary *tot = self.snap[@"totals"];
  NSDictionary *res = self.snap[@"resource"];
  double aiGb = [tot isKindOfClass:[NSDictionary class]] ? GbFromRssKb(tot[@"rss_kb"]) : 0;
  // Prefer headroom_mb (product truth); free_mb is diagnostic only.
  int headroomMb = -1;
  if ([res isKindOfClass:[NSDictionary class]] && res[@"headroom_mb"] != nil &&
      res[@"headroom_mb"] != [NSNull null])
    headroomMb = [res[@"headroom_mb"] intValue];
  NSString *hdrTitle;
  if (headroomMb >= 0)
    hdrTitle = [NSString stringWithFormat:@"AI tools  %.2f GB · headroom ~%d MB", aiGb, headroomMb];
  else
    hdrTitle = [NSString stringWithFormat:@"AI tools  %.2f GB", aiGb];

  NSMenuItem *hdr = [m addItemWithTitle:hdrTitle action:NULL keyEquivalent:@""];
  hdr.enabled = NO;

  // Resource interrupt
  if ([res isKindOfClass:[NSDictionary class]] && [res[@"show"] boolValue]) {
    [m addItem:[NSMenuItem separatorItem]];
    NSString *rt = res[@"title"] ?: @"Headroom pressure";
    NSMenuItem *ri = [m addItemWithTitle:rt action:NULL keyEquivalent:@""];
    ri.enabled = NO;
    NSString *det = res[@"detail"];
    if ([det isKindOfClass:[NSString class]] && det.length) {
      NSString *shortD = det.length > 90 ? [[det substringToIndex:87] stringByAppendingString:@"…"] : det;
      NSMenuItem *di = [m addItemWithTitle:shortD action:NULL keyEquivalent:@""];
      di.enabled = NO;
    }
    NSString *act = res[@"action_label"] ?: @"Reclaim idle";
    NSMenuItem *fr = [[NSMenuItem alloc] initWithTitle:act
                                                action:@selector(freeRam:)
                                         keyEquivalent:@""];
    fr.target = self;
    fr.enabled = YES;
    [m addItem:fr];
  }

  [m addItem:[NSMenuItem separatorItem]];
  NSMenuItem *sh = [m addItemWithTitle:@"Sessions (by RAM)" action:NULL keyEquivalent:@""];
  sh.enabled = NO;

  [self.sessionRows removeAllObjects];
  NSArray *sess = self.snap[@"sessions"];
  if ([sess isKindOfClass:[NSArray class]] && sess.count) {
    NSArray *sorted = [sess sortedArrayUsingComparator:^NSComparisonResult(id a, id b) {
      double ra = [a isKindOfClass:[NSDictionary class]] ? [a[@"rss_kb"] doubleValue] : 0;
      double rb = [b isKindOfClass:[NSDictionary class]] ? [b[@"rss_kb"] doubleValue] : 0;
      if (ra < rb)
        return NSOrderedDescending;
      if (ra > rb)
        return NSOrderedAscending;
      return NSOrderedSame;
    }];
    NSInteger i = 0;
    for (NSDictionary *s in sorted) {
      if (![s isKindOfClass:[NSDictionary class]])
        continue;
      [self.sessionRows addObject:s];
      NSString *app = s[@"app"] ?: @"?";
      NSString *lab = s[@"label"] ?: s[@"session_id"] ?: @"";
      NSString *act = s[@"activity_state"] ?: s[@"activity"] ?: @"";
      double gb = GbFromRssKb(s[@"rss_kb"]);
      NSString *line =
          [NSString stringWithFormat:@"%@  %.2f GB  %@%@", app, gb, lab,
                                     act.length ? [NSString stringWithFormat:@" · %@", act] : @""];
      if (line.length > 72)
        line = [[line substringToIndex:69] stringByAppendingString:@"…"];
      NSMenuItem *mi = [[NSMenuItem alloc] initWithTitle:line
                                                  action:@selector(endSession:)
                                           keyEquivalent:@""];
      mi.target = self;
      mi.tag = i;
      mi.enabled = YES;
      mi.toolTip = [NSString stringWithFormat:@"End session %@ / %@", app, s[@"session_id"] ?: @""];
      [m addItem:mi];
      i++;
      if (i >= 24)
        break;
    }
  } else {
    NSMenuItem *empty = [m addItemWithTitle:@"No AI sessions" action:NULL keyEquivalent:@""];
    empty.enabled = NO;
  }

  [m addItem:[NSMenuItem separatorItem]];
  NSMenuItem *ref = [[NSMenuItem alloc] initWithTitle:@"Refresh now"
                                               action:@selector(reload)
                                        keyEquivalent:@"r"];
  ref.target = self;
  [m addItem:ref];

  NSString *src = self.snap[@"source"] ?: @"?";
  NSMenuItem *srcItem =
      [m addItemWithTitle:[NSString stringWithFormat:@"Feed: %@ · %@", src,
                                                     [self.livePath lastPathComponent]]
                   action:NULL
            keyEquivalent:@""];
  srcItem.enabled = NO;

  [self addFooter:m];
  self.item.menu = m;
}

- (void)addFooter:(NSMenu *)m {
  [m addItem:[NSMenuItem separatorItem]];
  NSMenuItem *q = [[NSMenuItem alloc] initWithTitle:@"Quit Local AI Monitor menu"
                                             action:@selector(terminate:)
                                      keyEquivalent:@"q"];
  q.target = NSApp;
  [m addItem:q];
}

- (void)freeRam:(id)sender {
  (void)sender;
  NSDictionary *res = self.snap[@"resource"];
  NSString *label = @"heaviest idle AI session";
  if ([res isKindOfClass:[NSDictionary class]] && res[@"candidate_label"])
    label = [NSString stringWithFormat:@"%@", res[@"candidate_label"]];
  NSAlert *a = [NSAlert new];
  a.messageText = @"Reclaim idle load on this Mac?";
  a.informativeText = [NSString
      stringWithFormat:
          @"This stops %@.\nUnsaved work in that session may be lost.\n"
           @"Active mid-stream work is refused by policy.\n"
           @"Apple apps ≥1 GB may also quit under freeze-risk (by design).",
          label];
  [a addButtonWithTitle:@"Reclaim idle"];
  [a addButtonWithTitle:@"Cancel"];
  if ([a runModal] != NSAlertFirstButtonReturn)
    return;

  NSString *app = res[@"candidate_app"];
  NSString *sid = res[@"candidate_session_id"];
  if ([app isKindOfClass:[NSString class]] && app.length && [sid isKindOfClass:[NSString class]] &&
      sid.length) {
    [self runEndTool:app session:sid];
  } else {
    [self runHeaviest];
  }
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.8 * NSEC_PER_SEC)),
                 dispatch_get_main_queue(), ^{
                   [self reload];
                 });
}

- (void)endSession:(NSMenuItem *)sender {
  NSInteger tag = sender.tag;
  if (tag < 0 || tag >= (NSInteger)self.sessionRows.count)
    return;
  NSDictionary *s = self.sessionRows[(NSUInteger)tag];
  NSString *app = s[@"app"] ?: @"";
  NSString *sid = s[@"session_id"] ?: @"";
  NSString *lab = s[@"label"] ?: sid;
  NSAlert *a = [NSAlert new];
  a.messageText = [NSString stringWithFormat:@"End “%@”?", lab];
  a.informativeText = @"Stops this AI session tree. Active mid-stream work should be refused.";
  [a addButtonWithTitle:@"End session"];
  [a addButtonWithTitle:@"Cancel"];
  if ([a runModal] != NSAlertFirstButtonReturn)
    return;
  [self runEndTool:app session:sid];
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.8 * NSEC_PER_SEC)),
                 dispatch_get_main_queue(), ^{
                   [self reload];
                 });
}

- (NSString *)aiTopBin {
  NSString *home = NSHomeDirectory();
  NSArray *cands = @[
    [home stringByAppendingPathComponent:@".local/bin/local-ai-monitor"],
    @"/opt/homebrew/bin/local-ai-monitor",
    @"/usr/local/bin/local-ai-monitor",
  ];
  for (NSString *c in cands) {
    if ([[NSFileManager defaultManager] isExecutableFileAtPath:c])
      return c;
  }
  return @"local-ai-monitor";
}

- (void)runEndTool:(NSString *)app session:(NSString *)sid {
  if (!app.length || !sid.length) {
    [self runHeaviest];
    return;
  }
  NSTask *t = [NSTask new];
  t.launchPath = [self aiTopBin];
  t.arguments = @[ @"end-session", @"--tool", app, @"--session", sid ];
  t.environment = [[NSProcessInfo processInfo] environment];
  @try {
    [t launch];
    [t waitUntilExit];
  } @catch (NSException *ex) {
    NSLog(@"local-ai-monitor-litebar end-session failed: %@", ex);
    [self termPidsFromSessionApp:app sid:sid];
  }
}

- (void)runHeaviest {
  NSTask *t = [NSTask new];
  t.launchPath = [self aiTopBin];
  t.arguments = @[ @"end-session", @"--heaviest" ];
  @try {
    [t launch];
    [t waitUntilExit];
  } @catch (NSException *ex) {
    NSLog(@"heaviest failed: %@", ex);
  }
}

/* Fallback if local-ai-monitor CLI missing: SIGTERM pids from snapshot (catalog only). */
- (void)termPidsFromSessionApp:(NSString *)app sid:(NSString *)sid {
  for (NSDictionary *s in self.sessionRows) {
    if (![s[@"app"] isEqualToString:app] || ![s[@"session_id"] isEqualToString:sid])
      continue;
    NSArray *pids = s[@"pids"];
    if (![pids isKindOfClass:[NSArray class]])
      return;
    for (id p in pids) {
      int pid = [p intValue];
      if (pid > 1)
        kill(pid, SIGTERM);
    }
    return;
  }
}

@end

int main(int argc, const char *argv[]) {
  (void)argc;
  (void)argv;
  @autoreleasepool {
    [NSApplication sharedApplication];
    LiteBar *app = [LiteBar new];
    NSApp.delegate = app;
    [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
    [NSApp run];
  }
  return 0;
}
