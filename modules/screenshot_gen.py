"""Screenshot generation module"""
import logging
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from typing import Optional

logger = logging.getLogger("ig_monitor_bot")

class ScreenshotGenerator:
    def __init__(self):
        # Canvas dimensions - optimized for Instagram
        self.width = 1264
        self.height = 415
        self.background_color = '#000000'
        
        # Profile picture
        self.profile_pic_size = 240
        self.profile_pic_x = 40
        self.profile_pic_y = 62
        
        # Load fonts - all regular weight, no bold
        try:
            self.font_username = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
            self.font_stats_number = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 42)
            self.font_stats_label = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
            self.font_handle = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
            self.font_button = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
        except:
            logger.warning("Could not load custom fonts, using default")
            self.font_username = ImageFont.load_default()
            self.font_stats_number = ImageFont.load_default()
            self.font_stats_label = ImageFont.load_default()
            self.font_handle = ImageFont.load_default()
            self.font_button = ImageFont.load_default()
    
    @staticmethod
    def format_count(count: int) -> str:
        """Format follower/following count"""
        if count >= 1_000_000:
            return f"{count/1_000_000:.1f}M"
        elif count >= 1_000:
            return f"{count/1_000:.1f}K"
        return str(count)
    
    def _add_profile_picture(self, img: Image, draw: ImageDraw, image_data: Optional[bytes]):
        """Add profile picture to screenshot"""
        if image_data:
            try:
                profile_pic = Image.open(BytesIO(image_data)).resize(
                    (self.profile_pic_size, self.profile_pic_size),
                    Image.LANCZOS
                )
                
                # Create circular mask
                mask = Image.new('L', (self.profile_pic_size, self.profile_pic_size), 0)
                ImageDraw.Draw(mask).ellipse(
                    (0, 0, self.profile_pic_size, self.profile_pic_size), 
                    fill=255
                )
                
                # Apply mask
                output = Image.new('RGBA', (self.profile_pic_size, self.profile_pic_size), (0, 0, 0, 0))
                output.paste(profile_pic, (0, 0))
                output.putalpha(mask)
                
                img.paste(output, (self.profile_pic_x, self.profile_pic_y), output)
            except Exception as e:
                logger.error(f"Error processing profile picture: {e}")
                self._draw_placeholder_picture(draw)
        else:
            self._draw_placeholder_picture(draw)
    
    def _draw_placeholder_picture(self, draw: ImageDraw):
        """Draw placeholder for profile picture"""
        draw.ellipse(
            (self.profile_pic_x, self.profile_pic_y, 
             self.profile_pic_x + self.profile_pic_size, 
             self.profile_pic_y + self.profile_pic_size),
            fill='#262626', 
            outline='#555555', 
            width=3
        )
    
    def _add_header(self, draw: ImageDraw, username: str, is_verified: bool, verification_badge: Optional[bytes]):
        """Add username, verification badge, follow button and three-dot menu"""
        header_x = 370
        username_y = 75
        
        # Draw username
        draw.text((header_x, username_y), username, fill='#FFFFFF', font=self.font_username)
        
        # Get username width
        username_bbox = draw.textbbox((header_x, username_y), username, font=self.font_username)
        username_width = username_bbox[2] - username_bbox[0]
        
        current_x = header_x + username_width + 20
        
        # Add verification badge if verified
        if is_verified and verification_badge:
            try:
                badge_size = 65
                badge_img = Image.open(BytesIO(verification_badge)).convert('RGBA')
                badge_img = badge_img.resize((badge_size, badge_size), Image.LANCZOS)
                
                badge_x = current_x
                badge_y = username_y
                
                img = draw._image
                img.paste(badge_img, (badge_x, badge_y), badge_img)
                
                current_x += badge_size + 20
            except Exception as e:
                logger.error(f"Error adding verification badge: {e}")
        
        # Follow button
        button_width = 180
        button_height = 56
        button_radius = 10
        button_x = current_x
        button_y = username_y + 2
        
        draw.rounded_rectangle(
            (button_x, button_y, button_x + button_width, button_y + button_height),
            radius=button_radius,
            fill='#0095F6'
        )
        
        follow_text = "Follow"
        bbox = draw.textbbox((0, 0), follow_text, font=self.font_button)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        text_x = button_x + (button_width - text_width) // 2
        text_y = button_y + (button_height - text_height) // 2 - 2
        draw.text((text_x, text_y), follow_text, fill='#FFFFFF', font=self.font_button)
        
        current_x += button_width + 25
        
        # Three-dot menu
        menu_x = current_x
        menu_y = username_y + 28
        dot_radius = 3.5
        for i in range(3):
            center_x = menu_x + i * 13
            draw.ellipse(
                (center_x - dot_radius, menu_y - dot_radius, 
                 center_x + dot_radius, menu_y + dot_radius), 
                fill='#FFFFFF'
            )
    
    def _add_stats(self, draw: ImageDraw, followers: int, following: int, posts: int):
        """Add stats section"""
        stats_y = 160
        header_x = 370
        spacing = 290
        
        # Posts
        posts_x = header_x
        draw.text((posts_x, stats_y), str(posts), fill='#FFFFFF', font=self.font_stats_number)
        draw.text((posts_x, stats_y + 52), "posts", fill='#A8A8A8', font=self.font_stats_label)
        
        # Followers
        followers_x = posts_x + spacing
        draw.text((followers_x, stats_y), self.format_count(followers), fill='#FFFFFF', font=self.font_stats_number)
        draw.text((followers_x, stats_y + 52), "followers", fill='#A8A8A8', font=self.font_stats_label)
        
        # Following
        following_x = followers_x + spacing
        draw.text((following_x, stats_y), str(following), fill='#FFFFFF', font=self.font_stats_number)
        draw.text((following_x, stats_y + 52), "following", fill='#A8A8A8', font=self.font_stats_label)
    
    def _add_username_handle(self, draw: ImageDraw, username: str):
        """Add username handle at bottom"""
        handle_y = 275
        header_x = 370
        draw.text((header_x, handle_y), username, fill='#FFFFFF', font=self.font_handle)
    
    def create_screenshot(
        self, 
        username: str, 
        image_data: Optional[bytes],
        followers: int,
        following: int,
        posts: int,
        full_name: str,
        bio: str,
        is_verified: bool = False,
        verification_badge: Optional[bytes] = None
    ) -> Optional[BytesIO]:
        """Create Instagram profile screenshot
        
        Args:
            username: Instagram username
            image_data: Profile picture image bytes
            followers: Number of followers
            following: Number of following
            posts: Number of posts
            full_name: Full display name (not used)
            bio: Bio text (not used)
            is_verified: Whether the account is verified
            verification_badge: Verification badge image bytes
        """
        try:
            # Create base image
            img = Image.new('RGB', (self.width, self.height), color=self.background_color)
            draw = ImageDraw.Draw(img)
            
            # Store img reference for badge pasting
            draw._image = img
            
            # Add all elements
            self._add_profile_picture(img, draw, image_data)
            self._add_header(draw, username, is_verified, verification_badge)
            self._add_stats(draw, followers, following, posts)
            self._add_username_handle(draw, username)
            
            # Save to buffer
            output_buffer = BytesIO()
            img.save(output_buffer, format='PNG', optimize=True, quality=100)
            output_buffer.seek(0)
            
            return output_buffer
            
        except Exception as e:
            logger.error(f"Error creating screenshot: {e}")
            return None